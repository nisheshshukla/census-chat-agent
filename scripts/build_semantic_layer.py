"""Build the semantic layer in CENSUS_APP_DB.SEMANTIC and snapshot it to data/schema/.

Creates (idempotent, CREATE OR REPLACE):
  SEMANTIC.STATES              state name / abbreviation / FIPS reference
  SEMANTIC.FIELD_DESCRIPTIONS  one row per (vintage, column) with a readable path + flags
  SEMANTIC.GEO_CBG             block group -> state/county names, land area, lat/lon
  SEMANTIC.FIELD_SEARCH        Cortex Search service over FIELD_DESCRIPTIONS (estimates only)

Snapshots:
  data/schema/field_descriptions.json   for the local FTS fallback and tests
  data/schema/fips_codes.json           for the geography resolver

Usage: .venv/bin/python scripts/build_semantic_layer.py [--with-search]

Note: Cortex Search needs the EMBED_TEXT function, which Snowflake blocks on trial accounts
(error 399258). The app therefore defaults to the local FTS backend; --with-search is for a
paid account.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from census_agent.config import get_settings  # noqa: E402
from census_agent.data.snowflake_client import SnowflakeClient  # noqa: E402
from census_agent.data.states import STATES  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "schema"
SEM = "CENSUS_APP_DB.SEMANTIC"
NON_SUMMABLE = ["%median%", "%mean %", "%per capita%", "%ratio%", "%percent%", "%gini%"]


def acs_select(db: str, vintage: str) -> str:
    return f"""
    SELECT '{vintage}' AS VINTAGE, 'acs5' AS SOURCE,
           '{vintage}_CBG_' || LEFT(TABLE_NUMBER, 3) AS TABLE_NAME,
           TABLE_ID AS COLUMN_NAME, TABLE_NUMBER, TABLE_TITLE, TABLE_TOPICS AS TOPICS,
           TABLE_UNIVERSE AS UNIVERSE,
           CASE WHEN FIELD_LEVEL_1 = 'Estimate' THEN 'estimate' ELSE 'moe' END AS KIND,
           ARRAY_TO_STRING(ARRAY_CONSTRUCT_COMPACT(FIELD_LEVEL_4, FIELD_LEVEL_5, FIELD_LEVEL_6,
               FIELD_LEVEL_7, FIELD_LEVEL_8, "FIELD_LEVELl_9", FIELD_LEVEL_10), ' > ') AS PATH
    FROM {db}.PUBLIC."{vintage}_METADATA_CBG_FIELD_DESCRIPTIONS"
    """


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-search", action="store_true", help="also create the Cortex Search service")
    args = ap.parse_args()
    settings = get_settings()
    client = SnowflakeClient(settings)
    db = settings.snowflake_census_database or json.load(open(OUT / "tables.json"))["database"]
    print(f"share database: {db}")

    values = ", ".join(f"('{n.replace(chr(39), chr(39) * 2)}','{a}','{f}')" for n, a, f in STATES)
    await client.query(
        f"CREATE OR REPLACE TABLE {SEM}.STATES (STATE_NAME TEXT, STATE_ABBR TEXT, STATE_FIPS TEXT) "
        f"AS SELECT * FROM VALUES {values}"
    )
    print("STATES ok")

    nonsum = " OR ".join(f"TABLE_TITLE ILIKE '{p}'" for p in NON_SUMMABLE)
    await client.query(
        f"""
        CREATE OR REPLACE TABLE {SEM}.FIELD_DESCRIPTIONS AS
        WITH base AS (
            {acs_select(db, "2020")}
            UNION ALL
            {acs_select(db, "2019")}
            UNION ALL
            SELECT '2020' AS VINTAGE, 'decennial' AS SOURCE,
                   '2020_REDISTRICTING_CBG_DATA' AS TABLE_NAME,
                   COLUMN_ID AS COLUMN_NAME, LEFT(COLUMN_ID, 4) AS TABLE_NUMBER,
                   INITCAP(COLUMN_TOPIC) || ' (2020 Decennial Census)' AS TABLE_TITLE,
                   INITCAP(COLUMN_TOPIC) AS TOPICS, COLUMN_UNIVERSE AS UNIVERSE,
                   'count' AS KIND, FIELD_NAME AS PATH
            FROM {db}.PUBLIC."2020_REDISTRICTING_METADATA_CBG_FIELD_DESCRIPTIONS"
        )
        SELECT VINTAGE, SOURCE, TABLE_NAME, COLUMN_NAME, TABLE_NUMBER, TABLE_TITLE, TOPICS,
               UNIVERSE, KIND, PATH,
               NOT ({nonsum}) AS IS_SUMMABLE,
               TABLE_TITLE || ' | ' || COALESCE(PATH, '') || ' | universe: ' || COALESCE(UNIVERSE, '')
                   || ' | topics: ' || COALESCE(TOPICS, '') AS SEARCH_TEXT
        FROM base
        """
    )
    n = await client.query(f"SELECT COUNT(*), COUNT_IF(KIND <> 'moe') FROM {SEM}.FIELD_DESCRIPTIONS")
    print(f"FIELD_DESCRIPTIONS rows: {n.rows[0][0]} (searchable: {n.rows[0][1]})")

    await client.query(
        f"""
        CREATE OR REPLACE TABLE {SEM}.GEO_CBG AS
        WITH g AS (
            SELECT CENSUS_BLOCK_GROUP, AMOUNT_LAND, AMOUNT_WATER, LATITUDE, LONGITUDE, 2020 AS V
            FROM {db}.PUBLIC."2020_METADATA_CBG_GEOGRAPHIC_DATA"
            UNION ALL
            SELECT CENSUS_BLOCK_GROUP, AMOUNT_LAND, AMOUNT_WATER, LATITUDE, LONGITUDE, 2019 AS V
            FROM {db}.PUBLIC."2019_METADATA_CBG_GEOGRAPHIC_DATA"
        ),
        d AS (
            SELECT * FROM g
            QUALIFY ROW_NUMBER() OVER (PARTITION BY CENSUS_BLOCK_GROUP ORDER BY V DESC) = 1
        ),
        f AS (
            SELECT STATE_FIPS, COUNTY_FIPS, STATE, COUNTY FROM (
                SELECT STATE_FIPS, COUNTY_FIPS, STATE, COUNTY, 2020 AS V
                FROM {db}.PUBLIC."2020_METADATA_CBG_FIPS_CODES"
                UNION ALL
                SELECT STATE_FIPS, COUNTY_FIPS, STATE, COUNTY, 2019 AS V
                FROM {db}.PUBLIC."2019_METADATA_CBG_FIPS_CODES"
            )
            QUALIFY ROW_NUMBER() OVER (PARTITION BY STATE_FIPS, COUNTY_FIPS ORDER BY V DESC) = 1
        )
        SELECT d.CENSUS_BLOCK_GROUP,
               LEFT(d.CENSUS_BLOCK_GROUP, 2) AS STATE_FIPS,
               SUBSTR(d.CENSUS_BLOCK_GROUP, 3, 3) AS COUNTY_FIPS,
               SUBSTR(d.CENSUS_BLOCK_GROUP, 6, 6) AS TRACT_CODE,
               f.STATE AS STATE_ABBR, s.STATE_NAME, f.COUNTY AS COUNTY_NAME,
               d.AMOUNT_LAND, d.AMOUNT_WATER, d.LATITUDE, d.LONGITUDE, d.V AS LATEST_VINTAGE
        FROM d
        LEFT JOIN f ON f.STATE_FIPS = LEFT(d.CENSUS_BLOCK_GROUP, 2)
                   AND f.COUNTY_FIPS = SUBSTR(d.CENSUS_BLOCK_GROUP, 3, 3)
        LEFT JOIN {SEM}.STATES s ON s.STATE_FIPS = LEFT(d.CENSUS_BLOCK_GROUP, 2)
        """
    )
    n = await client.query(
        f"SELECT COUNT(*), COUNT_IF(COUNTY_NAME IS NULL), COUNT_IF(STATE_NAME IS NULL) FROM {SEM}.GEO_CBG"
    )
    print(f"GEO_CBG rows: {n.rows[0][0]} (no county: {n.rows[0][1]}, no state: {n.rows[0][2]})")

    fd = await client.query(
        f"""SELECT VINTAGE, SOURCE, TABLE_NAME, COLUMN_NAME, TABLE_NUMBER, TABLE_TITLE, TOPICS,
                   UNIVERSE, KIND, PATH, IS_SUMMABLE
            FROM {SEM}.FIELD_DESCRIPTIONS ORDER BY VINTAGE DESC, TABLE_NAME, COLUMN_NAME"""
    )
    rows = [dict(zip([c.lower() for c in fd.columns], r, strict=False)) for r in fd.rows]
    rows = [r for r in rows if r["kind"] != "moe"]
    with gzip.open(OUT / "field_descriptions.json.gz", "wt") as f:
        json.dump(rows, f, separators=(",", ":"))
    fips = await client.query(
        f"""SELECT DISTINCT STATE_FIPS, COUNTY_FIPS, STATE_ABBR, STATE_NAME, COUNTY_NAME
            FROM {SEM}.GEO_CBG WHERE COUNTY_NAME IS NOT NULL ORDER BY 1, 2"""
    )
    (OUT / "fips_codes.json").write_text(json.dumps([list(r) for r in fips.rows], separators=(",", ":")))
    print(f"snapshots: {len(rows)} field rows, {len(fips.rows)} counties")

    if not args.with_search:
        print("skipping Cortex Search service (pass --with-search on a non-trial account)")
        return
    t0 = time.time()
    try:
        await _create_search_service(client, settings.snowflake_warehouse)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "not available for trial accounts" in msg:
            print(
                "Cortex Search unavailable: trial accounts cannot run the embedding function. "
                "Keep FIELD_SEARCH_BACKEND=local."
            )
            return
        raise
    print(f"FIELD_SEARCH service created in {time.time() - t0:.0f}s; probing readiness")
    await _probe_search(client)
    print(f"done in {time.time() - t0:.0f}s")


async def _create_search_service(client: SnowflakeClient, warehouse: str) -> None:
    await client.query(
        f"""
        CREATE OR REPLACE CORTEX SEARCH SERVICE {SEM}.FIELD_SEARCH
          ON SEARCH_TEXT
          ATTRIBUTES VINTAGE, SOURCE, TABLE_NAME, KIND, IS_SUMMABLE
          WAREHOUSE = {warehouse}
          TARGET_LAG = '1 day'
          AS (
            SELECT SEARCH_TEXT, VINTAGE, SOURCE, TABLE_NAME, COLUMN_NAME, TABLE_NUMBER,
                   TABLE_TITLE, UNIVERSE, PATH, KIND, IS_SUMMABLE
            FROM {SEM}.FIELD_DESCRIPTIONS
            WHERE KIND <> 'moe'
          )
        """
    )


async def _probe_search(client: SnowflakeClient) -> None:
    for _ in range(40):
        try:
            r = await client.query(
                f"""SELECT PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
                        '{SEM}.FIELD_SEARCH',
                        '{{"query": "median household income", "columns": ["COLUMN_NAME", "TABLE_TITLE"], "limit": 3}}'
                    ))['results'] AS R"""
            )
            print("probe:", str(r.rows[0][0])[:300])
            break
        except Exception as exc:  # noqa: BLE001
            print("not ready:", str(exc)[:120])
            await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
