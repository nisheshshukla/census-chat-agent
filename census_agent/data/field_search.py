"""Field search: find Census columns by meaning.

Two backends behind one interface:
  CortexSearchBackend  Snowflake Cortex Search over FIELD_DESCRIPTIONS (hybrid semantic+keyword)
  LocalFtsBackend      SQLite FTS5 over the committed snapshot (deterministic; CI and fallback)

`FieldSearch` prefers Cortex when configured and falls back per request on error or timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from census_agent.data.catalog import SCHEMA_DIR, Catalog, FieldInfo
from census_agent.data.concepts import curated_matches
from census_agent.data.snowflake_client import SnowflakeClientProtocol

log = logging.getLogger(__name__)

STOPWORDS = {
    "the",
    "of",
    "in",
    "a",
    "an",
    "and",
    "or",
    "for",
    "to",
    "is",
    "are",
    "what",
    "how",
    "many",
    "much",
    "who",
    "which",
    "with",
    "by",
    "on",
    "at",
    "per",
    "number",
    "people",
    "county",
    "state",
    "does",
    "do",
    "have",
    "has",
    "live",
    "living",
    "there",
    "that",
    "this",
    "than",
    "about",
    "me",
    "tell",
    "show",
    "give",
    "was",
    "were",
    "rate",
    "rates",
    "percentage",
    "share",
    "count",
    "counts",
    "each",
    "all",
}
SYNONYMS = {
    "kids": "children under 18 years",
    "children": "children under 18 years",
    "seniors": "65 years and over",
    "elderly": "65 years and over",
    "old": "age",
    "earn": "income",
    "earnings": "income",
    "salary": "income",
    "wealth": "income",
    "poor": "poverty",
    "rich": "income 200,000 or more",
    "homes": "housing units",
    "houses": "housing units",
    "house": "housing",
    "rent": "gross rent renter",
    "renters": "renter occupied",
    "owners": "owner occupied",
    "commute": "transportation to work travel time",
    "commuters": "means of transportation to work",
    "bus": "public transportation bus",
    "car": "car truck or van",
    "drive": "car truck or van drove alone",
    "college": "bachelor's degree educational attainment",
    "degree": "educational attainment",
    "school": "school enrollment",
    "veterans": "veteran status",
    "immigrants": "foreign born nativity",
    "foreign": "foreign born nativity",
    "language": "language spoken at home",
    "spanish": "speak spanish",
    "internet": "internet subscription computer",
    "broadband": "broadband internet subscription",
    "insurance": "health insurance coverage",
    "uninsured": "no health insurance coverage",
    "married": "marital status",
    "divorced": "marital status divorced",
    "women": "female",
    "men": "male",
    "hispanic": "hispanic or latino",
    "latino": "hispanic or latino",
    "black": "black or african american",
    "asian": "asian alone",
    "white": "white alone",
    "native": "american indian and alaska native",
    "race": "race",
    "unemployed": "unemployed employment status",
    "jobs": "employment status",
    "work": "employment status worked",
    "disability": "disability status",
    "vehicles": "vehicles available",
    "mobile": "mobile home",
    "value": "home value owner occupied",
    "age": "age",
    "median": "median",
}


@dataclass(frozen=True)
class FieldHit:
    info: FieldInfo
    score: float
    backend: str

    def to_dict(self) -> dict[str, Any]:
        i = self.info
        return {
            "column": i.column_name,
            "table": i.table_name,
            "vintage": i.vintage,
            "source": i.source,
            "title": i.table_title,
            "path": i.path,
            "universe": i.universe,
            "summable": i.is_summable,
            "moe_column": i.moe_column,
            "score": round(self.score, 3),
        }


class FieldSearchBackend(Protocol):
    name: str

    async def search(self, query: str, k: int, vintage: str | None) -> list[FieldHit]: ...


def expand_query(q: str) -> str:
    """Drop stopwords, expand '500k' style numbers, and ADD synonyms (never replace)."""
    q = re.sub(r"\b(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m.group(1)) * 1000)), q.lower())
    q = re.sub(r"\b(over|more than|above|at least)\b", "or more", q)
    q = re.sub(r"\b(under|less than|below)\b", "less than", q)
    words = re.findall(r"[a-z0-9']+", q)
    out: list[str] = []
    for w in words:
        if w in STOPWORDS or len(w) <= 1:
            continue
        out.append(w)
        if w in SYNONYMS:
            out.append(SYNONYMS[w])
    return " ".join(out) or q


class LocalFtsBackend:
    name = "local_fts"

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE VIRTUAL TABLE f USING fts5(key UNINDEXED, vintage UNINDEXED, "
            "title, path, universe, topics, tokenize='porter unicode61')"
        )
        rows = [
            (
                f"{i.table_name}|{i.column_name}",
                i.vintage,
                i.table_title,
                i.path,
                i.universe,
                i.topics,
            )
            for i in catalog.fields.values()
            if i.kind != "moe"
        ]
        self.db.executemany("INSERT INTO f VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    async def search(self, query: str, k: int, vintage: str | None) -> list[FieldHit]:
        terms = [t for t in re.findall(r"[a-z0-9]+", expand_query(query).lower()) if t not in STOPWORDS]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))
        sql = (
            "SELECT key, vintage, bm25(f, 0, 0, 3.0, 2.0, 1.0, 0.5) AS s FROM f WHERE f MATCH ? "
            + ("AND vintage = ? " if vintage else "")
            + "ORDER BY s LIMIT ?"
        )
        params: list[Any] = [match] + ([vintage] if vintage else []) + [k * 4]
        try:
            rows = self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        hits: list[FieldHit] = []
        for key, _, s in rows:
            t, c = key.split("|")
            info = self.catalog.field(t, c)
            if info:
                hits.append(FieldHit(info, -float(s), self.name))
        return _prefer_totals(hits, query)[:k]


class CortexSearchBackend:
    name = "cortex_search"

    def __init__(
        self,
        client: SnowflakeClientProtocol,
        catalog: Catalog,
        service: str,
        timeout_s: float = 2.5,
    ) -> None:
        self.client = client
        self.catalog = catalog
        self.service = service
        self.timeout_s = timeout_s

    async def search(self, query: str, k: int, vintage: str | None) -> list[FieldHit]:
        payload: dict[str, Any] = {
            "query": expand_query(query),
            "columns": ["TABLE_NAME", "COLUMN_NAME"],
            "limit": k * 3,
        }
        if vintage:
            payload["filter"] = {"@eq": {"VINTAGE": vintage}}
        sql = f"SELECT PARSE_JSON(SNOWFLAKE.CORTEX.SEARCH_PREVIEW('{self.service}', %s))['results']"
        res = await asyncio.wait_for(self.client.query(sql, [json.dumps(payload)]), timeout=self.timeout_s)
        raw = res.rows[0][0] if res.rows else None
        results = json.loads(raw) if isinstance(raw, str) else (raw or [])
        hits: list[FieldHit] = []
        for rank, r in enumerate(results):
            info = self.catalog.field(r["TABLE_NAME"], r["COLUMN_NAME"])
            if info:
                hits.append(FieldHit(info, 1.0 / (rank + 1), self.name))
        return _prefer_totals(hits, query)[:k]


def _prefer_totals(hits: list[FieldHit], query: str = "") -> list[FieldHit]:
    """Re-rank: totals first, allocation-flag (B99) and Puerto-Rico-only tables last."""
    q = query.lower()

    def adjusted(h: FieldHit) -> float:
        s = h.score
        if h.info.column_name.endswith("e1") or h.info.path == "Total":
            s += 0.15 * max(1.0, abs(h.score))
        if h.info.table_number.startswith("B99") and "allocat" not in q:
            s -= 10.0
        if h.info.table_number.endswith("PR") and "puerto" not in q:
            s -= 10.0
        return s

    return sorted(hits, key=lambda h: -adjusted(h))


class FieldSearch:
    """Curated concepts first, then the search backend (Cortex if configured, else local)."""

    def __init__(self, primary: FieldSearchBackend | None, fallback: FieldSearchBackend, catalog: Catalog) -> None:
        self.primary = primary
        self.fallback = fallback
        self.catalog = catalog

    async def search(self, query: str, k: int = 8, vintage: str | None = "2020") -> tuple[list[FieldHit], str]:
        curated: list[FieldHit] = []
        for table, column in curated_matches(query):
            if table == "SEMANTIC":
                continue
            if vintage and vintage != "2020":
                table = table.replace("2020_", f"{vintage}_", 1)
            info = self.catalog.field(table, column)
            if info:
                curated.append(FieldHit(info, 2.0, "curated"))
        hits, backend = await self._backend_search(query, k, vintage)
        seen = {(h.info.table_name, h.info.column_name) for h in curated}
        merged = curated + [h for h in hits if (h.info.table_name, h.info.column_name) not in seen]
        return merged[: max(k, len(curated))], backend if not curated else f"curated+{backend}"

    async def _backend_search(self, query: str, k: int, vintage: str | None) -> tuple[list[FieldHit], str]:
        if self.primary is not None:
            t0 = time.perf_counter()
            try:
                hits = await self.primary.search(query, k, vintage)
                if hits:
                    return hits, self.primary.name
                log.info(
                    "field search primary returned nothing; using fallback",
                    extra={"extra_fields": {"query": query}},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "field search primary failed; using fallback",
                    extra={
                        "extra_fields": {
                            "backend": self.primary.name,
                            "error": str(exc)[:200],
                            "ms": int((time.perf_counter() - t0) * 1000),
                        }
                    },
                )
        return await self.fallback.search(query, k, vintage), self.fallback.name


def build_field_search(
    catalog: Catalog,
    snowflake: SnowflakeClientProtocol | None,
    backend: str,
    service: str = "CENSUS_APP_DB.SEMANTIC.FIELD_SEARCH",
) -> FieldSearch:
    local = LocalFtsBackend(catalog)
    primary: FieldSearchBackend | None = None
    if backend == "cortex" and snowflake is not None:
        primary = CortexSearchBackend(snowflake, catalog, service)
    return FieldSearch(primary, local, catalog)


def snapshot_exists(schema_dir: Path = SCHEMA_DIR) -> bool:
    return (schema_dir / "field_descriptions.json.gz").exists()
