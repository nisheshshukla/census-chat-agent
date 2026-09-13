"""Resolve place names in questions to Census geography (state / county FIPS).

Deterministic and unit-testable. Handles state names and abbreviations, county names with or
without a state qualifier, and reports ambiguity (thirty-one states have a Washington County)
instead of guessing. City names are not in the dataset; a county with the same name is offered
as the nearest available geography, flagged as such.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from census_agent.data.states import ABBR_TO_FIPS, ABBR_TO_NAME, NAME_TO_ABBR, STATES

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "data" / "schema"
_STATES_LONGEST_FIRST = sorted(STATES, key=lambda s: -len(s[0]))
_ABBR_RE = re.compile(r"(,\s*)?\b([A-Z]{2})\b")

COUNTY_WORDS = ("county", "parish", "borough", "municipio")
COUNTY_SUFFIXES = (
    " county",
    " parish",
    " borough",
    " census area",
    " municipality",
    " city and borough",
    " municipio",
    " city",
)
NATION_WORDS = {
    "us",
    "usa",
    "u s",
    "united states",
    "america",
    "the united states",
    "nationwide",
    "national",
    "the us",
    "the usa",
}
RISKY_ABBRS = {
    "IN",
    "OR",
    "ME",
    "OK",
    "HI",
    "DE",
    "AL",
    "ID",
    "CO",
    "PA",
    "LA",
    "MA",
    "MD",
    "MS",
    "MT",
    "NE",
    "AS",
}
PHRASE_STOP = {
    "of",
    "in",
    "for",
    "and",
    "vs",
    "versus",
    "the",
    "to",
    "at",
    "from",
    "with",
    "about",
    "on",
    "between",
    "compare",
    "is",
    "are",
    "what",
    "how",
    "many",
    "does",
    "do",
    "live",
}


@dataclass(frozen=True)
class Geo:
    level: str
    state_fips: str
    state_abbr: str
    state_name: str
    county_fips: str | None = None
    county_name: str | None = None

    def label(self) -> str:
        if self.level == "nation":
            return "United States"
        if self.level == "state":
            return self.state_name
        return f"{self.county_name}, {self.state_abbr}"

    def sql_filter(self, alias: str = "g") -> str:
        if self.level == "nation":
            return "1=1"
        if self.level == "state":
            return f"{alias}.STATE_FIPS = '{self.state_fips}'"
        return f"{alias}.STATE_FIPS = '{self.state_fips}' AND {alias}.COUNTY_FIPS = '{self.county_fips}'"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "level": self.level,
            "label": self.label(),
            "state_fips": self.state_fips or None,
            "state_abbr": self.state_abbr,
            "county_fips": self.county_fips,
        }


@dataclass
class Resolution:
    query: str
    status: str
    matches: list[Geo] = field(default_factory=list)
    note: str = ""

    @property
    def geo(self) -> Geo | None:
        return self.matches[0] if self.status == "resolved" else None

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "status": self.status,
            "note": self.note,
            "matches": [g.to_dict() for g in self.matches[:12]],
        }


def _norm(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[?!;:\"()]", " ", s)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("saint "):
        s = "st " + s[6:]
    return s


def _strip_suffix(s: str) -> str:
    for suf in COUNTY_SUFFIXES:
        if s.endswith(suf):
            return s[: -len(suf)].strip()
    return s


class GeographyResolver:
    def __init__(self, counties: list[tuple[str, str, str, str, str]]) -> None:
        self.counties: list[Geo] = [Geo("county", sf, sa, sn, cf, cn) for sf, cf, sa, sn, cn in counties]
        self.states: dict[str, Geo] = {f: Geo("state", f, a, n) for n, a, f in STATES}
        self._by_norm: dict[str, list[Geo]] = {}
        for g in self.counties:
            assert g.county_name is not None
            key = _strip_suffix(_norm(g.county_name))
            self._by_norm.setdefault(key, []).append(g)
        self._county_keys = list(self._by_norm)
        self.nation = Geo("nation", "", "US", "United States")

    def resolve_state(self, text: str) -> Geo | None:
        t = _norm(text)
        if t in NAME_TO_ABBR:
            return self.states[ABBR_TO_FIPS[NAME_TO_ABBR[t]]]
        if len(t) == 2 and t.upper() in ABBR_TO_NAME:
            return self.states[ABBR_TO_FIPS[t.upper()]]
        return None

    def resolve(self, text: str) -> Resolution:
        raw = text.strip()
        if not raw:
            return Resolution(raw, "unresolved", note="empty place name")
        if _norm(raw) in NATION_WORDS:
            return Resolution(raw, "resolved", [self.nation])

        state_hint: Geo | None = None
        place_raw, _, state_raw = raw.rpartition(",")
        if state_raw and place_raw:
            state_hint = self.resolve_state(state_raw)
            place = _norm(place_raw) if state_hint else _norm(raw)
        else:
            place = _norm(raw)
            parts = place.split()
            if len(parts) >= 2:
                for n_state_words in (2, 1):
                    if len(parts) > n_state_words:
                        tail = " ".join(parts[-n_state_words:])
                        head = parts[:-n_state_words]
                        st = self.resolve_state(tail)
                        if st and (head[-1] in COUNTY_WORDS or (len(tail) == 2 and len(head) >= 1)):
                            state_hint, place = st, " ".join(head)
                            break

        if state_hint is None:
            st = self.resolve_state(place)
            if st:
                return Resolution(raw, "resolved", [st])
        elif place in ("", "state"):
            return Resolution(raw, "resolved", [state_hint])

        key = _strip_suffix(place)
        matches = list(self._by_norm.get(key, []))
        fuzzy_note = ""
        if not matches:
            close = difflib.get_close_matches(key, self._county_keys, n=3, cutoff=0.86)
            for c in close:
                matches.extend(self._by_norm[c])
            if close:
                fuzzy_note = f"matched by similarity to '{close[0]}'"
        if state_hint:
            matches = [m for m in matches if m.state_fips == state_hint.state_fips]

        if not matches:
            note = "no state or county with this name; cities, towns, ZIP codes, and metro areas are not in the dataset"
            if state_hint:
                note += f" (state understood as {state_hint.state_name})"
            return Resolution(raw, "unresolved", note=note)
        if len(matches) == 1:
            note = fuzzy_note
            if not place.endswith(COUNTY_SUFFIXES) and not fuzzy_note:
                note = "interpreted as the county of that name; city-level data is not available"
            return Resolution(raw, "resolved", matches, note)
        return Resolution(
            raw,
            "ambiguous",
            sorted(matches, key=lambda g: g.state_name),
            note=f"{len(matches)} counties match; ask which state",
        )

    def find_places(self, text: str) -> list[Resolution]:
        """Scan a question for '<X> County[, State]' phrases, state names, and abbreviations."""
        found: list[Resolution] = []
        words = _norm(text).split()
        consumed: set[int] = set()

        for i, w in enumerate(words):
            if w not in COUNTY_WORDS:
                continue
            j = i - 1
            head: list[str] = []
            while j >= 0 and len(head) < 3 and words[j] not in PHRASE_STOP:
                head.insert(0, words[j])
                j -= 1
            if not head:
                continue
            span = set(range(i - len(head), i + 1))
            state_hint = ""
            for n in (2, 1):
                tail = " ".join(words[i + 1 : i + 1 + n])
                if tail and self.resolve_state(tail):
                    state_hint = tail
                    span |= set(range(i + 1, i + 1 + n))
                    break
            phrase = " ".join(head + [w]) + (f", {state_hint}" if state_hint else "")
            res = self.resolve(phrase)
            if res.status != "unresolved":
                found.append(res)
                consumed |= span

        for name, _abbr, fips in _STATES_LONGEST_FIRST:
            n_words = name.lower().split()
            for i in range(len(words) - len(n_words) + 1):
                if words[i : i + len(n_words)] == n_words and not (set(range(i, i + len(n_words))) & consumed):
                    consumed |= set(range(i, i + len(n_words)))
                    found.append(Resolution(name, "resolved", [self.states[fips]]))
                    break

        for m in _ABBR_RE.finditer(text):
            abbr = m.group(2)
            if abbr not in ABBR_TO_NAME or (abbr in RISKY_ABBRS and not m.group(1)):
                continue
            if any(r.geo and r.geo.state_abbr == abbr for r in found):
                continue
            found.append(Resolution(abbr, "resolved", [self.states[ABBR_TO_FIPS[abbr]]]))

        if not found and any(
            w in NATION_WORDS or f"{words[i - 1]} {w}" in NATION_WORDS for i, w in enumerate(words) if i > 0
        ):
            found.append(Resolution("United States", "resolved", [self.nation]))
        return found


@lru_cache
def load_resolver(schema_dir: Path = SCHEMA_DIR) -> GeographyResolver:
    rows = json.loads((schema_dir / "fips_codes.json").read_text())
    return GeographyResolver([(r[0], r[1], r[2], r[3], r[4]) for r in rows])
