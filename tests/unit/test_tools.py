from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from census_agent.agent.cache import TTLCache
from census_agent.agent.tools import TOOL_DEFINITIONS, ToolRunner
from census_agent.agent.trace import TurnTrace
from census_agent.config import Settings
from census_agent.data.catalog import load_catalog
from census_agent.data.field_search import build_field_search
from census_agent.data.geography import load_resolver
from census_agent.data.snowflake_client import QueryResult
from census_agent.errors import DataUnavailableError

DB = "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"
SQL = f'SELECT SUM("B01003e1") AS population FROM {DB}.PUBLIC."2020_CBG_B01"'


class Snow:
    def __init__(self, *, error: Exception | None = None, rows: int = 1) -> None:
        self.error = error
        self.calls = 0
        self.rows = rows

    async def query(self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int | None = None) -> QueryResult:
        self.calls += 1
        if self.error:
            raise self.error
        rows = [(float(i),) for i in range(self.rows)]
        return QueryResult(
            columns=["POPULATION"], rows=rows, elapsed_ms=5, truncated=max_rows is not None and self.rows > max_rows
        )

    async def ping(self) -> bool:
        return True


def runner(snow: Any = None, **overrides: Any) -> ToolRunner:
    settings = Settings(_env_file=None, anthropic_api_key="t", snowflake_account="X-Y", **overrides)
    catalog = load_catalog()
    return ToolRunner(settings, catalog, build_field_search(catalog, None, "local"), load_resolver(), snow, TTLCache())


def trace() -> TurnTrace:
    return TurnTrace(request_id="r", session_id="s", model="m")


def test_tool_definitions_are_strict_and_named() -> None:
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {"search_census_fields", "describe_table", "resolve_geography", "run_census_sql"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in TOOL_DEFINITIONS)


async def test_search_fields_tool() -> None:
    r = runner()
    out, err = await r.run("search_census_fields", {"query": "median household income", "vintage": "2020"}, trace())
    assert not err
    data = json.loads(out)
    assert data["results"][0]["column"] == "B19013e1" and data["backend"].endswith("local_fts")
    assert r.fields_seen


async def test_search_fields_no_match_note() -> None:
    out, err = await runner().run("search_census_fields", {"query": "zzzz qqqq", "vintage": "2019"}, trace())
    assert not err and json.loads(out)["results"] == [] and "may not be in the dataset" in out


async def test_describe_table_filters_and_unknown() -> None:
    r = runner()
    out, _ = await r.run(
        "describe_table", {"table_name": "2020_CBG_B01", "table_id": "B01001", "contains": "65"}, trace()
    )
    data = json.loads(out)
    assert data["total"] > 0 and all("65" in c["path"] or "65" in c["title"] for c in data["columns"])
    out, _ = await r.run("describe_table", {"table_name": "2020_cbg_b01", "table_id": "", "contains": ""}, trace())
    assert json.loads(out)["table"] == "2020_CBG_B01"
    out, _ = await r.run("describe_table", {"table_name": "NOPE", "table_id": "", "contains": ""}, trace())
    assert "unknown table" in json.loads(out)["error"]


async def test_resolve_geography_tool_records_result() -> None:
    r = runner()
    out, _ = await r.run("resolve_geography", {"name": "Cook County, IL"}, trace())
    assert json.loads(out)["status"] == "resolved" and r.geographies[0]["matches"][0]["label"] == "Cook County, IL"


async def test_run_sql_executes_then_caches() -> None:
    snow = Snow()
    r = runner(snow)
    t = trace()
    out1, err1 = await r.run("run_census_sql", {"sql": SQL, "purpose": "pop"}, t)
    out2, err2 = await r.run("run_census_sql", {"sql": SQL + "  ", "purpose": "pop again"}, t)
    assert not err1 and not err2 and snow.calls == 1
    assert json.loads(out1)["cached"] is False and json.loads(out2)["cached"] is True
    assert t.tool_calls[1].cached is True and len(r.executed) == 2


async def test_run_sql_zero_rows_and_truncation_notes() -> None:
    out, _ = await runner(Snow(rows=0)).run("run_census_sql", {"sql": SQL, "purpose": ""}, trace())
    assert "Zero rows" in json.loads(out)["note"]
    out, _ = await runner(Snow(rows=80)).run("run_census_sql", {"sql": SQL, "purpose": ""}, trace())
    data = json.loads(out)
    assert data["row_count"] == 80 and len(data["rows"]) == 60 and "first 60" in data["note"]


async def test_run_sql_validation_error_is_returned_not_raised() -> None:
    t = trace()
    out, err = await runner(Snow()).run("run_census_sql", {"sql": "DROP TABLE x", "purpose": ""}, t)
    assert err and out.startswith("Query rejected") and t.tool_calls[0].error.startswith("sql_invalid")


async def test_run_sql_snowflake_compile_error_is_sanitized() -> None:
    exc = Exception("000904 (42000): 01b2-0304: SQL compilation error: invalid identifier 'B01003E1'")
    out, err = await runner(Snow(error=exc)).run("run_census_sql", {"sql": SQL, "purpose": ""}, trace())
    assert err and out.startswith("Query failed in Snowflake: SQL compilation error") and "01b2-0304" not in out


async def test_run_sql_data_unavailable_propagates() -> None:
    with pytest.raises(DataUnavailableError):
        await runner(Snow(error=DataUnavailableError("down"))).run(
            "run_census_sql", {"sql": SQL, "purpose": ""}, trace()
        )


async def test_run_sql_without_snowflake_configured() -> None:
    with pytest.raises(DataUnavailableError):
        await runner(None).run("run_census_sql", {"sql": SQL, "purpose": ""}, trace())


async def test_unknown_tool_and_run_many() -> None:
    r = runner(Snow())
    out, err = await r.run("nope", {}, trace())
    assert err and "unknown tool" in out
    results = await r.run_many(
        [("a", "resolve_geography", {"name": "Texas"}), ("b", "run_census_sql", {"sql": SQL, "purpose": ""})], trace()
    )
    assert [rid for rid, _, _ in results] == ["a", "b"] and not any(e for _, _, e in results)
