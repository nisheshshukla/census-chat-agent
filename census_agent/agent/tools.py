"""Tool definitions (JSON schema, strict) and their implementations."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from census_agent.agent.cache import TTLCache, normalize_sql
from census_agent.agent.trace import ToolCallRecord, TurnTrace
from census_agent.config import Settings
from census_agent.data.catalog import Catalog
from census_agent.data.field_search import FieldSearch
from census_agent.data.geography import GeographyResolver
from census_agent.data.snowflake_client import QueryResult, SnowflakeClientProtocol
from census_agent.errors import DataUnavailableError, SQLInvalidError
from census_agent.guardrails.sql_validator import validate_sql

log = logging.getLogger(__name__)

MAX_ROWS_TO_MODEL = 60
MAX_CELL_CHARS = 120

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_census_fields",
        "description": (
            "Find Census columns by meaning (e.g. 'median household income', 'people who bike to "
            "work', 'housing units built before 1950'). Returns column ids with table, readable "
            "path, universe, whether the value is summable, and the paired margin-of-error column."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "concept to look for, in plain English"},
                "vintage": {
                    "type": "string",
                    "enum": ["2020", "2019"],
                    "description": "ACS vintage; 2020 unless comparing years",
                },
            },
            "required": ["query", "vintage"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_table",
        "description": (
            "List the estimate columns of one Census table with their readable paths, e.g. all "
            "age-by-sex cells of 2020_CBG_B01 for table id B01001. Use `contains` to filter by a "
            "word in the path (e.g. 'Female', '65')."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "e.g. 2020_CBG_B01"},
                "table_id": {
                    "type": "string",
                    "description": "ACS table id prefix, e.g. B01001; empty for all",
                },
                "contains": {
                    "type": "string",
                    "description": "substring filter on the path; empty for none",
                },
            },
            "required": ["table_name", "table_id", "contains"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_geography",
        "description": (
            "Resolve a place name to Census geography: state or county with FIPS codes. Reports "
            "ambiguity (several counties share a name) and unknown places (cities, ZIPs)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_census_sql",
        "description": (
            "Run one read-only Snowflake SELECT against the Census share. Column and table names "
            "must be double-quoted. Returns columns, rows (truncated for display), row count, and "
            "elapsed time. Validation errors explain exactly what to fix."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "purpose": {"type": "string", "description": "one line: what this query answers"},
            },
            "required": ["sql", "purpose"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ExecutedQuery:
    sql: str
    purpose: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    cached: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "purpose": self.purpose,
            "columns": self.columns,
            "rows": self.rows[:MAX_ROWS_TO_MODEL],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "cached": self.cached,
        }


def _cell(v: Any) -> Any:
    if isinstance(v, bool | int | float) or v is None:
        return v
    s = str(v)
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"


class ToolRunner:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        field_search: FieldSearch,
        resolver: GeographyResolver,
        snowflake: SnowflakeClientProtocol | None,
        sql_cache: TTLCache[QueryResult],
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.field_search = field_search
        self.resolver = resolver
        self.snowflake = snowflake
        self.sql_cache = sql_cache
        self.executed: list[ExecutedQuery] = []
        self.geographies: list[dict[str, Any]] = []
        self.fields_seen: list[dict[str, Any]] = []

    async def run(self, name: str, args: dict[str, Any], trace: TurnTrace) -> tuple[str, bool]:
        """Returns (content_for_model, is_error)."""
        t0 = time.perf_counter()
        rec = ToolCallRecord(name=name, ms=0)
        trace.tool_calls.append(rec)
        try:
            if name == "search_census_fields":
                out = await self._search(args["query"], args.get("vintage") or "2020", trace)
            elif name == "describe_table":
                out = self._describe(args["table_name"], args.get("table_id", ""), args.get("contains", ""))
            elif name == "resolve_geography":
                out = self._resolve(args["name"])
            elif name == "run_census_sql":
                out, rec.cached = await self._run_sql(args["sql"], args.get("purpose", ""))
            else:
                raise SQLInvalidError(f"unknown tool {name}")
            rec.summary = out[:120].replace("\n", " ")
            return out, False
        except SQLInvalidError as exc:
            rec.error = f"sql_invalid: {exc.detail[:200]}"
            return f"Query rejected: {exc.detail}", True
        except DataUnavailableError as exc:
            rec.error = f"data_unavailable: {exc.detail[:200]}"
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            rec.error = f"{type(exc).__name__}: {msg[:200]}"
            log.warning("tool %s failed: %s", name, msg[:300])
            return f"Query failed in Snowflake: {_sanitize_sf_error(msg)}", True
        finally:
            rec.ms = int((time.perf_counter() - t0) * 1000)

    async def _search(self, query: str, vintage: str, trace: TurnTrace) -> str:
        hits, backend = await self.field_search.search(query, k=8, vintage=vintage)
        trace.search_backend = backend
        payload = [h.to_dict() for h in hits]
        self.fields_seen.extend(payload)
        if not payload:
            return json.dumps(
                {
                    "results": [],
                    "note": "no matching columns; this concept may not be in the dataset",
                }
            )
        return json.dumps({"results": payload, "backend": backend})

    def _describe(self, table_name: str, table_id: str, contains: str) -> str:
        table_name = table_name.strip().strip('"')
        if not self.catalog.has_share_table(table_name):
            close = [t for t in self.catalog.share_table_names() if t.upper() == table_name.upper()]
            if close:
                table_name = close[0]
            else:
                return json.dumps(
                    {
                        "error": f"unknown table {table_name}",
                        "tables": self.catalog.share_table_names()[:80],
                    }
                )
        fields = self.catalog.fields_for_table(table_name)
        if table_id:
            fields = [f for f in fields if f.column_name.upper().startswith(table_id.upper())]
        if contains:
            fields = [
                f for f in fields if contains.lower() in f.path.lower() or contains.lower() in f.table_title.lower()
            ]
        total = len(fields)
        fields = fields[:120]
        rows = [
            {
                "column": f.column_name,
                "title": f.table_title,
                "path": f.path,
                "summable": f.is_summable,
            }
            for f in fields
        ]
        return json.dumps({"table": table_name, "shown": len(rows), "total": total, "columns": rows})

    def _resolve(self, name: str) -> str:
        res = self.resolver.resolve(name)
        d = res.to_dict()
        self.geographies.append(d)
        return json.dumps(d)

    async def _run_sql(self, sql: str, purpose: str) -> tuple[str, bool]:
        validated = validate_sql(sql, self.catalog, self.settings.max_result_rows)
        if self.snowflake is None:
            raise DataUnavailableError("snowflake not configured")
        key = normalize_sql(validated.sql)
        cached = self.sql_cache.get(key)
        if cached is not None:
            result, was_cached = cached, True
        else:
            result = await self.snowflake.query(validated.sql, max_rows=self.settings.max_result_rows)
            self.sql_cache.set(key, result)
            was_cached = False
        rows = [[_cell(v) for v in r] for r in result.rows]
        eq = ExecutedQuery(
            sql=validated.sql,
            purpose=purpose,
            columns=result.columns,
            rows=rows,
            row_count=len(rows),
            truncated=result.truncated,
            elapsed_ms=0 if was_cached else result.elapsed_ms,
            cached=was_cached,
        )
        self.executed.append(eq)
        note = ""
        if eq.row_count == 0:
            note = "Zero rows. Likely a geography filter that matched nothing or a NULL-only measure; check FIPS codes."
        elif eq.row_count > MAX_ROWS_TO_MODEL:
            note = (
                f"Only the first {MAX_ROWS_TO_MODEL} of {eq.row_count} rows are shown; "
                "aggregate or add ORDER BY/LIMIT if you need a specific subset."
            )
        return json.dumps({**eq.to_dict(), "note": note}, default=str), was_cached

    async def run_many(
        self, calls: list[tuple[str, str, dict[str, Any]]], trace: TurnTrace
    ) -> list[tuple[str, str, bool]]:
        """Execute tool calls concurrently. Returns (tool_use_id, content, is_error)."""
        results = await asyncio.gather(*(self.run(n, a, trace) for _, n, a in calls))
        return [(tid, content, err) for (tid, _, _), (content, err) in zip(calls, results, strict=True)]


def _sanitize_sf_error(msg: str) -> str:
    for marker in (
        "SQL compilation error:",
        "SQL execution error:",
        "Statement reached its statement or warehouse timeout",
    ):
        if marker in msg:
            return marker + msg.split(marker, 1)[1][:300]
    return msg[:300]
