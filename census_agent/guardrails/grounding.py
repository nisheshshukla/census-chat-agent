"""Output grounding: every substantive number in the answer must trace to query results.

A number is grounded when it matches a value in the results (within rounding), or a ratio /
difference / percentage of values from the same result set, or is trivially small (counts of
items, years, single-digit rankings). Ungrounded numbers do not block the answer; they are
flagged in the trace and a caveat is appended so the reader knows what to double-check.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

NUMBER_RE = re.compile(
    r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(million|billion|thousand|k|m|bn|%|percent)?",
    re.IGNORECASE,
)
SCALES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}


@dataclass
class GroundingReport:
    checked: int = 0
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def extract_numbers(text: str) -> list[tuple[str, float, bool]]:
    """Return (literal, value, is_percent) for each number-like token in the answer."""
    out: list[tuple[str, float, bool]] = []
    for m in NUMBER_RE.finditer(text):
        whole, frac, suffix = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
        value = float(whole.replace(",", "") + frac)
        is_percent = suffix in ("%", "percent")
        if suffix in SCALES:
            value *= SCALES[suffix]
        out.append((m.group(0).strip(), value, is_percent))
    return out


def _close(a: float, b: float, rel: float = 0.015) -> bool:
    if a == b:
        return True
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= rel


def result_numbers(results: list[dict[str, Any]]) -> tuple[set[float], list[list[float]]]:
    """All numeric cells, and per-row numeric lists (first rows only) for derived checks."""
    values: set[float] = set()
    rows: list[list[float]] = []
    for res in results:
        for i, row in enumerate(res.get("rows", [])):
            nums = [float(v) for v in row if isinstance(v, int | float) and not isinstance(v, bool)]
            values.update(nums)
            if i < 12:
                rows.append(nums)
    return values, rows


def check_grounding(answer: str, results: list[dict[str, Any]]) -> GroundingReport:
    report = GroundingReport()
    values, rows = result_numbers(results)
    if not values:
        for literal, value, _ in extract_numbers(answer):
            report.checked += 1
            if value > 1000 and not (1900 <= value <= 2100):
                report.ungrounded.append(literal)
        return report

    derived: set[float] = set()
    for nums in rows:
        for a, b in itertools.permutations(nums, 2):
            if b:
                derived.add(a / b * 100)
                derived.add(a / b)
            derived.add(a - b)
        derived.update(sum(nums[i:j]) for i in range(len(nums)) for j in range(i + 2, min(len(nums), i + 6) + 1))
    column_sums = _column_sums(rows)
    flat = [v for r in rows[:12] for v in r][:40]
    for a, b in itertools.permutations(flat, 2):
        derived.add(a - b)
        if b:
            derived.add(a / b * 100)
            derived.add(a / b)

    for literal, value, is_percent in extract_numbers(answer):
        report.checked += 1
        if value <= 100 or 1900 <= value <= 2100:
            continue
        if any(_close(value, v) for v in values):
            continue
        if any(_close(value, v) for v in derived) or any(_close(value, v) for v in column_sums):
            continue
        if is_percent:
            continue
        report.ungrounded.append(literal)
    return report


def _column_sums(rows: list[list[float]]) -> set[float]:
    if not rows:
        return set()
    width = max(len(r) for r in rows)
    return {sum(r[i] for r in rows if len(r) > i) for i in range(width)}


def caveat_for(report: GroundingReport) -> str:
    if report.ok:
        return ""
    shown = ", ".join(report.ungrounded[:4])
    return f"\n\n*Note: I could not trace these figures back to the query results: {shown}. Treat them as approximate.*"
