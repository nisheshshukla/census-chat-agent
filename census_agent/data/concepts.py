"""Curated concept map: the handful of measures almost every question starts from.

Search over 8,000 columns is the long tail. These entries guarantee that the most common
concepts resolve to the canonical column first (for example, "population" must mean B01003e1,
not one of the 160 age-by-sex cells). Every column here is verified against the catalog by a
unit test, so a stale entry fails CI rather than a demo.
"""

from __future__ import annotations

import re

CONCEPTS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        r"^(?!.*\b(65|under|over|age|aged|hispanic|black|white|asian|race|foreign|born)\b)"
        r".*\b(total )?population\b",
        [("2020_CBG_B01", "B01003e1")],
    ),
    (
        r"\b(2020 )?(decennial|census count|census population|official count)\b",
        [("2020_REDISTRICTING_CBG_DATA", "P0010001")],
    ),
    (r"\bmedian (household )?income\b", [("2020_CBG_B19", "B19013e1")]),
    (r"\bmedian family income\b", [("2020_CBG_B19", "B19113e1")]),
    (r"\bper capita income\b", [("2020_CBG_B19", "B19301e1")]),
    (r"\bmedian age\b", [("2020_CBG_B01", "B01002e1")]),
    (
        r"\bmedian (home|house|housing|property) value\b|\bhome values?\b|\bhouse prices?\b",
        [("2020_CBG_B25", "B25077e1")],
    ),
    (r"\bmedian (gross )?rent\b|\brents?\b", [("2020_CBG_B25", "B25064e1")]),
    (r"\bhousing units\b|\bhomes\b|\bhouses\b", [("2020_CBG_B25", "B25001e1")]),
    (
        r"\bpoverty\b|\bpoor\b|\bbelow (the )?poverty\b",
        [("2020_CBG_C17", "C17002e2"), ("2020_CBG_C17", "C17002e3"), ("2020_CBG_C17", "C17002e1")],
    ),
    (r"\bunemploy", [("2020_CBG_B23", "B23025e5"), ("2020_CBG_B23", "B23025e3")]),
    (
        r"\blabor force\b|\bemployed\b|\bemployment\b",
        [("2020_CBG_B23", "B23025e2"), ("2020_CBG_B23", "B23025e4"), ("2020_CBG_B23", "B23025e1")],
    ),
    (r"\bhispanic\b|\blatino\b", [("2020_CBG_B03", "B03003e3"), ("2020_CBG_B03", "B03003e1")]),
    (r"\b(black|african american)\b", [("2020_CBG_B02", "B02001e3"), ("2020_CBG_B02", "B02001e1")]),
    (r"\bwhite\b", [("2020_CBG_B02", "B02001e2"), ("2020_CBG_B03", "B03002e3")]),
    (r"\basian\b", [("2020_CBG_B02", "B02001e5")]),
    (r"\b(native american|american indian|alaska native)\b", [("2020_CBG_B02", "B02001e4")]),
    (r"\brace\b", [("2020_CBG_B02", "B02001e1")]),
    (r"\bveteran", [("2020_CBG_B21", "B21001e2"), ("2020_CBG_B21", "B21001e1")]),
    (
        r"\b(bachelor'?s?|college degree|college educated)\b",
        [("2020_CBG_B15", "B15003e22"), ("2020_CBG_B15", "B15003e1")],
    ),
    (r"\b(high school|graduat)", [("2020_CBG_B15", "B15003e17"), ("2020_CBG_B15", "B15003e1")]),
    (r"\b(health insurance|uninsured|insured)\b", [("2020_CBG_B27", "B27010e1")]),
    (
        r"\b(commute|commuting|transportation to work|drive to work|public transit)\b",
        [
            ("2020_CBG_B08", "B08301e1"),
            ("2020_CBG_B08", "B08301e10"),
            ("2020_CBG_B08", "B08301e21"),
        ],
    ),
    (r"\b(households?)\b(?!.*income)", [("2020_CBG_B11", "B11001e1")]),
    (
        r"\b(renters?|renter[- ]occupied)\b",
        [("2020_CBG_B25", "B25003e3"), ("2020_CBG_B25", "B25003e1")],
    ),
    (
        r"\b(home ?owners?|owner[- ]occupied|own their home)\b",
        [("2020_CBG_B25", "B25003e2"), ("2020_CBG_B25", "B25003e1")],
    ),
    (r"\bvacan", [("2020_CBG_B25", "B25002e3"), ("2020_CBG_B25", "B25002e1")]),
    (r"\b(snap|food stamps?)\b", [("2020_CBG_B22", "B22010e2"), ("2020_CBG_B22", "B22010e1")]),
    (r"\b(internet|broadband)\b", [("2020_CBG_B28", "B28002e2"), ("2020_CBG_B28", "B28002e1")]),
    (
        r"\b(spanish)\b",
        [
            ("2020_CBG_B16", "B16004e4"),
            ("2020_CBG_B16", "B16004e26"),
            ("2020_CBG_B16", "B16004e48"),
            ("2020_CBG_B16", "B16004e1"),
        ],
    ),
    (r"\b(children|kids|under 18)\b", [("2020_CBG_B01", "B01001e1"), ("2020_CBG_B09", "B09002e1")]),
    (r"\b(65 and over|seniors?|elderly|older adults|retire)", [("2020_CBG_B01", "B01001e1")]),
    (r"\b(age|ages|sex|gender|male|female|men|women)\b", [("2020_CBG_B01", "B01001e1")]),
    (r"\b(land area|density|square miles?|sq ?mi)\b", [("SEMANTIC", "GEO_CBG.AMOUNT_LAND")]),
]


_COMPILED = [(re.compile(pattern), cols) for pattern, cols in CONCEPTS]


def curated_matches(query: str) -> list[tuple[str, str]]:
    q = query.lower()
    out: list[tuple[str, str]] = []
    for pattern, cols in _COMPILED:
        if pattern.search(q):
            for c in cols:
                if c not in out:
                    out.append(c)
    return out
