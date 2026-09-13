"""In-memory sessions with TTL, a rolling message window, and a structured context card."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_TURNS_VERBATIM = 3
MAX_TURNS_TOTAL = 10
MAX_HISTORY_CHARS = 28000


@dataclass
class ContextCard:
    last_geographies: list[str] = field(default_factory=list)
    last_measures: list[str] = field(default_factory=list)
    last_sql: str = ""
    last_result_preview: str = ""
    last_vintage: str = "2020"

    def render(self) -> str:
        if not (self.last_geographies or self.last_measures or self.last_sql):
            return "(first turn; no prior context)"
        parts = []
        if self.last_geographies:
            parts.append("last geographies: " + "; ".join(self.last_geographies[-4:]))
        if self.last_measures:
            parts.append("last measures: " + "; ".join(self.last_measures[-4:]))
        if self.last_vintage:
            parts.append(f"last vintage: {self.last_vintage}")
        if self.last_sql:
            parts.append("last SQL:\n" + self.last_sql[:1200])
        if self.last_result_preview:
            parts.append("last result preview: " + self.last_result_preview[:400])
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_geographies": self.last_geographies[-4:],
            "last_measures": self.last_measures[-4:],
            "last_vintage": self.last_vintage,
            "last_sql": self.last_sql,
        }


@dataclass
class TurnRecord:
    question: str
    answer: str
    messages: list[dict[str, Any]]
    queries: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    error_kind: str | None = None
    suggestions: list[str] = field(default_factory=list)
    judge: dict[str, Any] | None = None
    created: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turns: list[TurnRecord] = field(default_factory=list)
    card: ContextCard = field(default_factory=ContextCard)

    def touch(self) -> None:
        self.last_used = time.time()

    def history_messages(self) -> list[dict[str, Any]]:
        """Recent turns verbatim, older turns as compact text, capped by characters."""
        turns = self.turns[-MAX_TURNS_TOTAL:]
        verbatim = turns[-MAX_TURNS_VERBATIM:]
        older = turns[: len(turns) - len(verbatim)]
        out: list[dict[str, Any]] = []
        if older:
            summary = "\n".join(f"- Q: {t.question[:200]}\n  A: {t.answer[:300]}" for t in older)
            out.append({"role": "user", "content": f"Earlier in this conversation:\n{summary}"})
            out.append({"role": "assistant", "content": "Understood, I have that context."})
        for t in verbatim:
            out.extend(t.messages)
        total = _chars(out)
        while total > MAX_HISTORY_CHARS and len(out) > 2:
            dropped = out[:2] if out[0]["role"] == "user" and isinstance(out[0]["content"], str) else out[:1]
            out = out[len(dropped) :]
            total -= _chars(dropped)
        return out

    def recent_context_text(self) -> str:
        if not self.turns:
            return ""
        t = self.turns[-1]
        return f"Previous question: {t.question[:300]}\nPrevious answer: {t.answer[:300]}"


def _chars(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = m["content"]
        n += len(c) if isinstance(c, str) else sum(len(str(b)) for b in c)
    return n


class SessionStore:
    def __init__(self, ttl_s: float = 7200.0, max_sessions: int = 2000, repository: Any = None) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self.repository = repository
        self._s: dict[str, Session] = {}

    async def acquire(self, session_id: str | None) -> Session:
        if session_id and session_id not in self._s and self.repository is not None:
            loaded = await self.repository.load(session_id)
            if loaded is not None:
                self._s[session_id] = loaded
        return self.get_or_create(session_id)

    async def find(self, session_id: str) -> Session | None:
        s = self.get(session_id)
        if s is None and self.repository is not None:
            s = await self.repository.load(session_id)
            if s is not None:
                self._s[session_id] = s
        return s

    def persist(self, session: Session) -> None:
        if self.repository is not None:
            self.repository.save_later(session)

    async def discard(self, session_id: str) -> Session:
        if self.repository is not None:
            await self.repository.delete(session_id)
        return self.reset(session_id)

    def get_or_create(self, session_id: str | None) -> Session:
        self._sweep()
        if session_id and session_id in self._s:
            s = self._s[session_id]
            s.touch()
            return s
        sid = session_id if session_id and _valid_id(session_id) else uuid.uuid4().hex
        s = Session(id=sid)
        self._s[sid] = s
        return s

    def get(self, session_id: str) -> Session | None:
        s = self._s.get(session_id)
        if s and time.time() - s.last_used > self.ttl_s:
            del self._s[session_id]
            return None
        return s

    def reset(self, session_id: str) -> Session:
        self._s.pop(session_id, None)
        return self.get_or_create(session_id)

    def _sweep(self) -> None:
        now = time.time()
        expired = [k for k, s in self._s.items() if now - s.last_used > self.ttl_s]
        for k in expired:
            del self._s[k]
        if len(self._s) > self.max_sessions:
            for k in sorted(self._s, key=lambda k: self._s[k].last_used)[: len(self._s) - self.max_sessions]:
                del self._s[k]

    def __len__(self) -> int:
        return len(self._s)


def _valid_id(s: str) -> bool:
    return 8 <= len(s) <= 64 and all(c.isalnum() or c in "-_" for c in s)
