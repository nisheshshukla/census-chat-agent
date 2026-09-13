"""Per-turn trace: what happened, how long each stage took, what it cost."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRecord:
    name: str
    ms: int
    cached: bool = False
    error: str | None = None
    summary: str = ""


@dataclass
class TurnTrace:
    request_id: str
    session_id: str
    model: str
    started: float = field(default_factory=time.perf_counter)
    stages: list[tuple[str, int]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    search_backend: str = ""
    classifier_category: str | None = None
    guardrail: str | None = None
    grounding_ungrounded: list[str] = field(default_factory=list)
    error_kind: str | None = None
    fallback_model: str | None = None
    first_token_ms: int | None = None
    model_ttft_ms: list[int] = field(default_factory=list)
    speculative: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    _stage_started: float = field(default_factory=time.perf_counter)

    def stage(self, name: str) -> None:
        now = time.perf_counter()
        self.stages.append((name, int((now - self._stage_started) * 1000)))
        self._stage_started = now

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def add_usage(self, usage: Any) -> None:
        if usage is None:
            return
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "stages": [{"name": n, "ms": ms} for n, ms in self.stages],
            "tool_calls": [
                {
                    "name": t.name,
                    "ms": t.ms,
                    "cached": t.cached,
                    "error": t.error,
                    "summary": t.summary,
                }
                for t in self.tool_calls
            ],
            "model_calls": self.model_calls,
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cache_read": self.cache_read_tokens,
                "cache_write": self.cache_write_tokens,
            },
            "search_backend": self.search_backend,
            "classifier": self.classifier_category,
            "guardrail": self.guardrail,
            "grounding_ungrounded": self.grounding_ungrounded,
            "error_kind": self.error_kind,
            "fallback_model": self.fallback_model,
            "first_token_ms": self.first_token_ms,
            "model_ttft_ms": self.model_ttft_ms,
            "speculative": self.speculative,
            "config": self.config,
        }
