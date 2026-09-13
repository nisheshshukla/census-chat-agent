"""Introspect the Census Marketplace share and write a schema snapshot to data/schema/.

Outputs:
  data/schema/tables.json   every table with columns, types, row counts
  data/schema/samples.json  first rows of the small "metadata-looking" tables
  data/schema/SUMMARY.md    human-readable overview for the worklog and README

Usage: .venv/bin/python scripts/introspect.py [--database NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from census_agent.config import get_settings  # noqa: E402
from census_agent.data.snowflake_client import SnowflakeClient  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "schema"


async def discover_database(client: SnowflakeClient, override: str | None) -> str:
    if override:
        return override
    res = await client.query("SHOW DATABASES LIKE 'US_OPEN_CENSUS%'")
    names = [r[res.columns.index("name")] for r in res.rows]
    if not names:
        raise SystemExit("No database starting with US_OPEN_CENSUS found. Is the share mounted?")
    return names[0]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database")
    args = ap.parse_args()

    settings = get_settings()
    client = SnowflakeClient(settings)
    db = await discover_database(client, args.database or settings.snowflake_census_database)
    print(f"database: {db}")

    cols = await client.query(
        f"""
        SELECT table_schema, table_name, column_name, data_type, ordinal_position
        FROM {db}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema <> 'INFORMATION_SCHEMA'
        ORDER BY table_schema, table_name, ordinal_position
        """
    )
    tables: dict[str, dict[str, Any]] = {}
    for schema, table, col, dtype, _ in cols.rows:
        key = f"{schema}.{table}"
        tables.setdefault(key, {"schema": schema, "table": table, "columns": []})
        tables[key]["columns"].append({"name": col, "type": dtype})
    print(f"tables: {len(tables)}, columns: {len(cols.rows)}")

    rc = await client.query(
        f"""
        SELECT table_schema, table_name, row_count, table_type
        FROM {db}.INFORMATION_SCHEMA.TABLES
        WHERE table_schema <> 'INFORMATION_SCHEMA'
        """
    )
    for schema, table, n, ttype in rc.rows:
        key = f"{schema}.{table}"
        if key in tables:
            tables[key]["row_count"] = n
            tables[key]["table_type"] = ttype

    samples: dict[str, Any] = {}
    for key, t in tables.items():
        if len(t["columns"]) <= 12:
            res = await client.query(f'SELECT * FROM {db}."{t["schema"]}"."{t["table"]}" LIMIT 5')
            samples[key] = {"columns": res.columns, "rows": [list(map(str, r)) for r in res.rows]}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tables.json").write_text(
        json.dumps({"database": db, "tables": tables}, indent=1, sort_keys=True, default=str)
    )
    (OUT / "samples.json").write_text(json.dumps(samples, indent=1, sort_keys=True))

    lines = [f"# Schema summary for `{db}`", "", "| table | columns | rows |", "|---|---:|---:|"]
    for key in sorted(tables):
        t = tables[key]
        lines.append(f"| {key} | {len(t['columns'])} | {t.get('row_count', '?')} |")
    lines += ["", "## Small tables (sampled)", ""]
    for key in sorted(samples):
        lines.append(f"### {key}")
        lines.append("")
        lines.append("columns: " + ", ".join(samples[key]["columns"]))
        lines.append("")
        for row in samples[key]["rows"][:3]:
            lines.append("- " + " | ".join(row)[:300])
        lines.append("")
    (OUT / "SUMMARY.md").write_text("\n".join(lines))
    print(f"wrote {OUT}/tables.json, samples.json, SUMMARY.md")


if __name__ == "__main__":
    asyncio.run(main())
