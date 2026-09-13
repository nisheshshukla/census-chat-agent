from __future__ import annotations

import pytest

from census_agent.data.geography import GeographyResolver, load_resolver


@pytest.fixture(scope="module")
def resolver() -> GeographyResolver:
    return load_resolver()


@pytest.mark.parametrize(
    "text, label",
    [
        ("California", "California"),
        ("CA", "California"),
        ("district of columbia", "District of Columbia"),
        ("Puerto Rico", "Puerto Rico"),
        ("Cook County, IL", "Cook County, IL"),
        ("cook county illinois", "Cook County, IL"),
        ("Harris County Texas", "Harris County, TX"),
        ("Kings County, NY", "Kings County, NY"),
        ("Miami-Dade", "Miami-Dade County, FL"),
        ("US", "United States"),
        ("United States", "United States"),
    ],
)
def test_resolves(resolver: GeographyResolver, text: str, label: str) -> None:
    r = resolver.resolve(text)
    assert r.status == "resolved", r
    assert r.geo is not None and r.geo.label() == label


def test_city_maps_to_county_with_note(resolver: GeographyResolver) -> None:
    r = resolver.resolve("Los Angeles")
    assert r.status == "resolved"
    assert r.geo is not None and r.geo.label() == "Los Angeles County, CA"
    assert "city-level" in r.note


def test_ambiguous_county_lists_candidates(resolver: GeographyResolver) -> None:
    r = resolver.resolve("Washington County")
    assert r.status == "ambiguous"
    assert len(r.matches) > 20
    assert all(g.county_name.startswith("Washington") for g in r.matches)  # incl. Washington Parish, LA


def test_state_named_washington_is_not_a_county(resolver: GeographyResolver) -> None:
    r = resolver.resolve("Washington")
    assert r.status == "resolved" and r.geo is not None and r.geo.level == "state"


def test_unknown_place_is_unresolved(resolver: GeographyResolver) -> None:
    r = resolver.resolve("Springfield")
    assert r.status == "unresolved"
    assert "ZIP" in r.note


def test_sql_filter(resolver: GeographyResolver) -> None:
    g = resolver.resolve("Cook County, IL").geo
    assert g is not None
    assert g.sql_filter() == "g.STATE_FIPS = '17' AND g.COUNTY_FIPS = '031'"
    assert resolver.resolve("Texas").geo.sql_filter("x") == "x.STATE_FIPS = '48'"  # type: ignore[union-attr]
    assert resolver.resolve("US").geo.sql_filter() == "1=1"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("What is the population of California?", ["California"]),
        (
            "How many people live in Cook County, IL vs Harris County, TX?",
            ["Cook County, IL", "Harris County, TX"],
        ),
        ("Compare Texas and Florida", ["Florida", "Texas"]),
        ("and for West Virginia?", ["West Virginia"]),
        ("How many people live in the US?", ["United States"]),
        ("population of Los Angeles county", ["Los Angeles County, CA"]),
        ("population of Springfield", []),
        ("Is Oregon bigger than Idaho", ["Oregon", "Idaho"]),
    ],
)
def test_find_places(resolver: GeographyResolver, question: str, expected: list[str]) -> None:
    labels = [r.geo.label() for r in resolver.find_places(question) if r.geo]
    assert sorted(labels) == sorted(expected)


def test_find_places_reports_ambiguity(resolver: GeographyResolver) -> None:
    found = resolver.find_places("median income in Washington County")
    assert len(found) == 1 and found[0].status == "ambiguous"


def test_uppercase_word_that_is_also_an_abbreviation_is_ignored(
    resolver: GeographyResolver,
) -> None:
    # "IN" and "OR" as English words must not become Indiana / Oregon.
    assert resolver.find_places("HOW MANY PEOPLE IN TOTAL OR NOT") == []
