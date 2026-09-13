"""Column catalog loaded from the committed schema snapshot.

Used by the SQL validator (does this table/column exist?) and by tools (pair an estimate
column with its margin-of-error column, look up a column's description).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "data" / "schema"

SEMANTIC_SCHEMA = "CENSUS_APP_DB.SEMANTIC"
SEMANTIC_TABLES: dict[str, list[str]] = {
    "GEO_CBG": [
        "CENSUS_BLOCK_GROUP",
        "STATE_FIPS",
        "COUNTY_FIPS",
        "TRACT_CODE",
        "STATE_ABBR",
        "STATE_NAME",
        "COUNTY_NAME",
        "AMOUNT_LAND",
        "AMOUNT_WATER",
        "LATITUDE",
        "LONGITUDE",
        "LATEST_VINTAGE",
    ],
    "STATES": ["STATE_NAME", "STATE_ABBR", "STATE_FIPS"],
    "FIELD_DESCRIPTIONS": [
        "VINTAGE",
        "SOURCE",
        "TABLE_NAME",
        "COLUMN_NAME",
        "TABLE_NUMBER",
        "TABLE_TITLE",
        "TOPICS",
        "UNIVERSE",
        "KIND",
        "PATH",
        "IS_SUMMABLE",
        "SEARCH_TEXT",
    ],
}


@dataclass(frozen=True)
class FieldInfo:
    vintage: str
    source: str
    table_name: str
    column_name: str
    table_number: str
    table_title: str
    topics: str
    universe: str
    kind: str
    path: str
    is_summable: bool

    @property
    def moe_column(self) -> str | None:
        if self.kind == "estimate":
            return self.column_name.replace("e", "m", 1) if "e" in self.column_name else None
        return None

    def label(self) -> str:
        p = f": {self.path}" if self.path else ""
        return f"{self.table_title}{p} (universe: {self.universe})"


@dataclass
class Catalog:
    database: str
    tables: dict[str, dict[str, str]]
    fields: dict[tuple[str, str], FieldInfo]
    _by_table: dict[str, list[FieldInfo]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for info in self.fields.values():
            self._by_table.setdefault(info.table_name, []).append(info)
        for infos in self._by_table.values():
            infos.sort(key=lambda f: _col_sort_key(f.column_name))

    def table_columns(self, schema: str, table: str) -> dict[str, str] | None:
        """Columns for a share table (schema PUBLIC) or a semantic table (schema SEMANTIC)."""
        if schema.upper() == "SEMANTIC":
            cols = SEMANTIC_TABLES.get(table.upper())
            return {c: "TEXT" for c in cols} if cols else None
        return self.tables.get(f"{schema.upper()}.{table}")

    def share_table_names(self) -> list[str]:
        return sorted(k.split(".", 1)[1] for k in self.tables)

    def has_share_table(self, table: str) -> bool:
        return f"PUBLIC.{table}" in self.tables

    def field(self, table_name: str, column_name: str) -> FieldInfo | None:
        return self.fields.get((table_name, column_name))

    def fields_for_table(self, table_name: str, *, kind: str | None = "estimate") -> list[FieldInfo]:
        infos = self._by_table.get(table_name, [])
        return [f for f in infos if f.kind == kind] if kind else list(infos)


def _col_sort_key(col: str) -> tuple[str, int]:
    head = col.rstrip("0123456789")
    tail = col[len(head) :]
    return head, int(tail) if tail.isdigit() else 0


@lru_cache
def load_catalog(schema_dir: Path = SCHEMA_DIR) -> Catalog:
    raw = json.loads((schema_dir / "tables.json").read_text())
    tables: dict[str, dict[str, str]] = {}
    for key, t in raw["tables"].items():
        tables[key] = {c["name"]: c["type"] for c in t["columns"]}
    fields: dict[tuple[str, str], FieldInfo] = {}
    fd_path = schema_dir / "field_descriptions.json.gz"
    if fd_path.exists():
        with gzip.open(fd_path, "rt") as f:
            raw_fields = json.load(f)
        for r in raw_fields:
            info = FieldInfo(
                vintage=str(r["vintage"]),
                source=r["source"],
                table_name=r["table_name"],
                column_name=r["column_name"],
                table_number=r["table_number"] or "",
                table_title=r["table_title"] or "",
                topics=r["topics"] or "",
                universe=r["universe"] or "",
                kind=r["kind"],
                path=r["path"] or "",
                is_summable=bool(r["is_summable"]),
            )
            fields[(info.table_name, info.column_name)] = info
    return Catalog(database=raw["database"], tables=tables, fields=fields)
