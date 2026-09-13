"""SQL validator: the only path from the model to Snowflake.

Allows exactly one read-only SELECT over the Census share and the semantic schema, checks
that every referenced table and column exists in the catalog (with a precise message when a
column name has the wrong case), rejects SELECT * on wide tables, and enforces a LIMIT.
The Snowflake role is read-only anyway; this layer exists so a bad query fails locally in
milliseconds with an explanation the model can act on, not after a round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from census_agent.data.catalog import SEMANTIC_TABLES, Catalog
from census_agent.errors import SQLInvalidError

SEMANTIC_DB = "CENSUS_APP_DB"
SEMANTIC_SCHEMA = "SEMANTIC"
WIDE_TABLE_COLUMNS = 30
FORBIDDEN_FUNCTION_PREFIXES = ("SYSTEM$", "RESULT_SCAN", "GET_DDL", "TO_FILE", "GET_STAGE")


@dataclass
class ResolvedTable:
    schema: str
    name: str
    columns: dict[str, str]

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class ValidatedSQL:
    sql: str
    tables: list[str] = field(default_factory=list)
    limit: int = 0


def _ident_name(node: exp.Expression | None) -> str:
    return node.name if node is not None else ""


def _resolve_table(catalog: Catalog, node: exp.Table) -> ResolvedTable:
    name, schema, db = node.name, node.db, node.catalog
    share_db = catalog.database.upper()
    s, d = schema.upper(), db.upper()
    if d == share_db or (d == "" and s == "PUBLIC"):
        if s not in ("PUBLIC", ""):
            raise SQLInvalidError(f'unknown schema "{schema}" in the Census database; use PUBLIC')
        cols = catalog.table_columns("PUBLIC", name)
        if cols is None:
            close = [t for t in catalog.share_table_names() if t.upper() == name.upper()]
            hint = f'; did you mean "{close[0]}"' if close else ""
            raise SQLInvalidError(
                f'unknown table "{name}" in {share_db}.PUBLIC{hint}. Table names must be '
                'double-quoted, e.g. "2020_CBG_B01"'
            )
        if close_name := next((t for t in catalog.share_table_names() if t == name), None):
            name = close_name
        return ResolvedTable("PUBLIC", name, cols)
    if (d in (SEMANTIC_DB, "") and s in (SEMANTIC_SCHEMA, "")) and name.upper() in SEMANTIC_TABLES:
        cols = catalog.table_columns("SEMANTIC", name.upper())
        assert cols is not None
        return ResolvedTable("SEMANTIC", name.upper(), cols)
    if d == "" and s == "":
        raise SQLInvalidError(
            f'table "{name}" is not fully qualified. Use {share_db}.PUBLIC."<table>" for Census '
            f"tables or {SEMANTIC_DB}.{SEMANTIC_SCHEMA}.GEO_CBG for geography"
        )
    raise SQLInvalidError(f"table {db}.{schema}.{name} is outside the allowed databases ({share_db}, {SEMANTIC_DB})")


def _check_column(table: ResolvedTable, col: exp.Column) -> None:
    ident = col.this
    name = col.name
    quoted = bool(getattr(ident, "quoted", False))
    cols = table.columns
    if quoted:
        if name in cols:
            return
    else:
        if name in cols or name.upper() in cols:
            return
    ci = [c for c in cols if c.upper() == name.upper()]
    if ci:
        raise SQLInvalidError(
            f'column {name} not found in "{table.name}"; did you mean "{ci[0]}"? Census column '
            "names are case-sensitive and must be double-quoted."
        )
    prefix = name[:6].upper()
    near = [c for c in cols if c.upper().startswith(prefix)][:5]
    hint = f" Similar columns: {', '.join(near)}." if near else ""
    raise SQLInvalidError(f'column "{name}" does not exist in "{table.name}".{hint}')


def validate_sql(sql: str, catalog: Catalog, max_rows: int) -> ValidatedSQL:
    try:
        statements = sqlglot.parse(sql, read="snowflake")
    except ParseError as exc:
        raise SQLInvalidError(f"could not parse SQL: {str(exc).splitlines()[0][:200]}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLInvalidError(f"exactly one statement is allowed, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLInvalidError(f"only SELECT statements are allowed, got {type(tree).__name__.upper()}")

    for fn in tree.find_all(exp.Func):
        fname = (fn.sql_name() if not isinstance(fn, exp.Anonymous) else fn.name).upper()
        if fname.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise SQLInvalidError(f"function {fname} is not allowed")

    cte_names = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE)}
    subquery_aliases = {s.alias_or_name.upper() for s in tree.find_all(exp.Subquery) if s.alias_or_name}
    scopes: dict[str, ResolvedTable] = {}
    resolved_tables: list[ResolvedTable] = []
    for t in tree.find_all(exp.Table):
        if not t.db and not t.catalog and t.name.upper() in cte_names:
            continue
        rt = _resolve_table(catalog, t)
        resolved_tables.append(rt)
        scopes[t.alias_or_name.upper()] = rt
        scopes.setdefault(t.name.upper(), rt)

    if not resolved_tables:
        raise SQLInvalidError("the query does not read any Census table")

    output_aliases = {a.alias.upper() for a in tree.find_all(exp.Alias) if a.alias}
    unique_tables = list({rt.qualified: rt for rt in resolved_tables}.values())
    for col in tree.find_all(exp.Column):
        qualifier = col.table.upper()
        if qualifier:
            if qualifier in cte_names or qualifier in subquery_aliases:
                continue
            scope = scopes.get(qualifier)
            if scope is None:
                raise SQLInvalidError(f'unknown table alias "{col.table}" for column {col.name}')
            _check_column(scope, col)
            continue
        if col.name.upper() in output_aliases:
            continue
        if len(unique_tables) == 1 or not any(
            col.name in rt.columns or col.name.upper() in rt.columns for rt in unique_tables
        ):
            _check_column(unique_tables[0], col)

    for star in tree.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Select | exp.Column):
            wide = [rt for rt in unique_tables if len(rt.columns) > WIDE_TABLE_COLUMNS]
            if wide:
                raise SQLInvalidError(
                    f"SELECT * is not allowed on wide tables ({wide[0].name} has "
                    f"{len(wide[0].columns)} columns); name the columns you need"
                )

    limit_node = tree.args.get("limit")
    limit_value = max_rows
    if isinstance(limit_node, exp.Limit) and isinstance(limit_node.expression, exp.Literal):
        try:
            limit_value = min(int(limit_node.expression.this), max_rows)
        except ValueError:
            limit_value = max_rows
    tree = tree.limit(limit_value)

    return ValidatedSQL(
        sql=tree.sql(dialect="snowflake", pretty=True),
        tables=sorted({rt.qualified for rt in unique_tables}),
        limit=limit_value,
    )
