from __future__ import annotations

import pytest

from census_agent.data.catalog import load_catalog
from census_agent.errors import SQLInvalidError
from census_agent.guardrails.sql_validator import validate_sql

DB = "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"
GEO = "CENSUS_APP_DB.SEMANTIC.GEO_CBG"
catalog = load_catalog()


def ok(sql: str, max_rows: int = 5000):
    return validate_sql(sql, catalog, max_rows)


def rejected(sql: str) -> str:
    with pytest.raises(SQLInvalidError) as ei:
        validate_sql(sql, catalog, 5000)
    return ei.value.detail


def test_state_population_pattern_passes_and_gets_limit() -> None:
    v = ok(
        f'SELECT g.STATE_NAME, SUM(t."B01003e1") AS population FROM {DB}.PUBLIC."2020_CBG_B01" t '
        f"JOIN {GEO} g ON g.CENSUS_BLOCK_GROUP = t.CENSUS_BLOCK_GROUP WHERE g.STATE_FIPS = '06' GROUP BY 1"
    )
    assert v.sql.rstrip().endswith("LIMIT 5000")
    assert v.tables == ["PUBLIC.2020_CBG_B01", "SEMANTIC.GEO_CBG"]
    assert '"B01003e1"' in v.sql  # quoting preserved


def test_existing_limit_is_capped() -> None:
    v = ok(f'SELECT "B01003e1" FROM {DB}.PUBLIC."2020_CBG_B01" LIMIT 999999', max_rows=100)
    assert v.sql.endswith("LIMIT 100") and v.limit == 100


def test_wrong_case_column_gets_precise_hint() -> None:
    msg = rejected(f'SELECT SUM(B01003E1) FROM {DB}.PUBLIC."2020_CBG_B01"')
    assert 'did you mean "B01003e1"' in msg


def test_unknown_column_lists_similar() -> None:
    msg = rejected(f'SELECT "B01003e9" FROM {DB}.PUBLIC."2020_CBG_B01"')
    assert "does not exist" in msg and "B01003e1" in msg


def test_unknown_table_rejected() -> None:
    msg = rejected(f'SELECT 1 FROM {DB}.PUBLIC."2020_CBG_B05"')
    assert "unknown table" in msg


def test_unqualified_table_rejected() -> None:
    assert "not fully qualified" in rejected('SELECT "B01003e1" FROM "2020_CBG_B01"')


def test_semantic_table_can_be_unqualified() -> None:
    v = ok("SELECT STATE_NAME, COUNT(*) FROM GEO_CBG GROUP BY 1")
    assert v.tables == ["SEMANTIC.GEO_CBG"]


def test_other_database_rejected() -> None:
    assert "outside the allowed" in rejected("SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY")


@pytest.mark.parametrize(
    "sql",
    [
        f'DELETE FROM {DB}.PUBLIC."2020_CBG_B01"',
        "DROP TABLE CENSUS_APP_DB.SEMANTIC.GEO_CBG",
        f'SELECT 1 FROM {DB}.PUBLIC."2020_CBG_B01"; SELECT 2',
        "CALL SYSTEM$ABORT_SESSION(1)",
        "SELECT SYSTEM$CANCEL_ALL_QUERIES(1) FROM GEO_CBG",
        "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    ],
)
def test_non_select_and_dangerous_rejected(sql: str) -> None:
    rejected(sql)


def test_select_star_on_wide_table_rejected() -> None:
    assert "SELECT *" in rejected(f'SELECT * FROM {DB}.PUBLIC."2020_CBG_B25"')


def test_select_star_on_narrow_table_ok() -> None:
    ok("SELECT * FROM CENSUS_APP_DB.SEMANTIC.STATES")


def test_cte_and_subquery_aliases_are_understood() -> None:
    v = ok(
        f'WITH s AS (SELECT LEFT(CENSUS_BLOCK_GROUP, 2) AS st, SUM("B01003e1") AS pop '
        f'FROM {DB}.PUBLIC."2020_CBG_B01" GROUP BY 1) '
        "SELECT s.st, s.pop FROM s ORDER BY s.pop DESC LIMIT 5"
    )
    assert v.limit == 5


def test_bad_alias_rejected() -> None:
    assert "unknown table alias" in rejected(f'SELECT x."B01003e1" FROM {DB}.PUBLIC."2020_CBG_B01" t')


def test_parse_error_is_reported() -> None:
    assert "could not parse" in rejected("SELEC FROM WHERE")


def test_moe_and_two_vintages() -> None:
    ok(
        f'SELECT (SELECT SUM("B01003e1") FROM {DB}.PUBLIC."2019_CBG_B01") AS a, '
        f'(SELECT SQRT(SUM(POWER("B01003m1", 2))) FROM {DB}.PUBLIC."2020_CBG_B01") AS b'
    )
