"""Deterministic input rules. Run before any model call; sub-millisecond."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RuleKind = Literal["ok", "empty", "too_long", "injection", "raw_sql"]

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|your) (instructions|rules|prompts?)",
    r"disregard (all |any |the )?(previous|prior|above|your) (instructions|rules)",
    r"\byou are now\b.*\b(dan|unrestricted|jailbroken|free)\b",
    r"\b(reveal|print|show|repeat|output)\b.*\b(system prompt|your instructions|hidden prompt)\b",
    r"\bpretend (you are|to be)\b.*\b(not|no longer)\b.*\b(assistant|bound|restricted)\b",
    r"\b(developer|admin|sudo|root) mode\b",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
]
DESTRUCTIVE_SQL = re.compile(
    r"\b(drop|delete|truncate|alter|insert|update|grant|revoke|create|merge|copy)\b"
    r"[\s\S]{0,40}\b(table|database|schema|warehouse|user|role|view|from|into)\b",
    re.IGNORECASE,
)
SELECT_SQL = re.compile(r"^\s*(with\b[\s\S]+?)?select\b[\s\S]+\bfrom\b", re.IGNORECASE)


@dataclass(frozen=True)
class RuleVerdict:
    kind: RuleKind
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.kind in ("empty", "too_long", "injection")


def check_input(text: str, max_chars: int) -> RuleVerdict:
    stripped = text.strip()
    if not stripped:
        return RuleVerdict("empty", "message is empty")
    if len(stripped) > max_chars:
        return RuleVerdict("too_long", f"message has {len(stripped)} characters; limit {max_chars}")
    lowered = stripped.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, lowered):
            return RuleVerdict("injection", f"matched injection pattern: {pat}")
    if DESTRUCTIVE_SQL.search(stripped):
        return RuleVerdict("injection", "destructive SQL statement in input")
    if SELECT_SQL.search(stripped):
        return RuleVerdict("raw_sql", "user supplied SQL; the agent writes its own queries")
    return RuleVerdict("ok")
