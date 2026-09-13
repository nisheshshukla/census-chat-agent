from __future__ import annotations

import pytest

from census_agent.data.catalog import Catalog, load_catalog
from census_agent.data.concepts import CONCEPTS, curated_matches
from census_agent.data.field_search import (
    FieldSearch,
    FieldSearchBackend,
    LocalFtsBackend,
    build_field_search,
    expand_query,
)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog()


@pytest.fixture(scope="module")
def search(catalog: Catalog) -> FieldSearch:
    return build_field_search(catalog, None, "local")


def test_catalog_loads_share_and_fields(catalog: Catalog) -> None:
    assert catalog.database.startswith("US_OPEN_CENSUS")
    assert catalog.has_share_table("2020_CBG_B01")
    assert catalog.table_columns("PUBLIC", "2020_CBG_B01")["B01003e1"] in ("NUMBER", "FLOAT")  # type: ignore[index]
    assert catalog.table_columns("SEMANTIC", "GEO_CBG") is not None
    assert catalog.table_columns("PUBLIC", "NOPE") is None
    assert len(catalog.fields) > 8000


def test_field_info_and_moe_pairing(catalog: Catalog) -> None:
    f = catalog.field("2020_CBG_B19", "B19013e1")
    assert f is not None
    assert f.moe_column == "B19013m1"
    assert f.is_summable is False  # median
    pop = catalog.field("2020_CBG_B01", "B01003e1")
    assert pop is not None and pop.is_summable is True and pop.moe_column == "B01003m1"
    dec = catalog.field("2020_REDISTRICTING_CBG_DATA", "P0010001")
    assert dec is not None and dec.kind == "count" and dec.moe_column is None


def test_every_curated_concept_column_exists(catalog: Catalog) -> None:
    missing = [(t, c) for _, cols in CONCEPTS for t, c in cols if t != "SEMANTIC" and catalog.field(t, c) is None]
    assert missing == []


@pytest.mark.parametrize(
    "q, expected_first",
    [
        ("What is the population of Texas?", "B01003e1"),
        ("median household income", "B19013e1"),
        ("how many people are below the poverty line", "C17002e2"),
        ("unemployment rate", "B23025e5"),
        ("hispanic population", "B03003e3"),
        ("2020 decennial census count", "P0010001"),
    ],
)
def test_curated_matches(q: str, expected_first: str) -> None:
    assert curated_matches(q)[0][1] == expected_first


def test_expand_query_adds_synonyms_and_numbers() -> None:
    q = expand_query("homes worth over 500k")
    assert "500000" in q and "housing units" in q and "homes" in q


@pytest.mark.parametrize(
    "q, expected_column",
    [
        ("people who take the bus to work", "B08301e11"),
        ("median age", "B01002e1"),
        ("bachelor's degree", "B15003e22"),
        ("veterans", "B21001e2"),
        ("earnings", "B19051e1"),
    ],
)
async def test_local_search_recall(search: FieldSearch, q: str, expected_column: str) -> None:
    hits, _ = await search.search(q, 5)
    assert expected_column in [h.info.column_name for h in hits], [h.info.column_name for h in hits]


async def test_allocation_tables_rank_last(catalog: Catalog) -> None:
    hits = await LocalFtsBackend(catalog).search("foreign born", 5, "2020")
    # Only allocation flags mention "foreign born" in this share; they still come back, flagged by table.
    assert all(h.info.table_number.startswith("B99") for h in hits)


async def test_total_population_is_curated_first(search: FieldSearch) -> None:
    hits, backend = await search.search("total population", 5)
    assert hits[0].info.column_name == "B01003e1"
    assert backend.startswith("curated+")


class _FailingBackend:
    name = "boom"

    async def search(self, query: str, k: int, vintage: str | None):  # noqa: ANN202
        raise RuntimeError("cortex down")


async def test_fallback_when_primary_fails(catalog: Catalog) -> None:
    primary: FieldSearchBackend = _FailingBackend()  # type: ignore[assignment]
    fs = FieldSearch(primary, LocalFtsBackend(catalog), catalog)
    hits, backend = await fs.search("median age", 3)
    assert backend == "curated+local_fts"
    assert hits[0].info.column_name == "B01002e1"
