"""LLM-as-judge: a second model checks an answer against the rows the agent retrieved."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)

JUDGE_SYSTEM = """You audit answers produced by a data assistant over US Census data. You are given the user's question, the SQL results the assistant retrieved (columns and rows), and the assistant's answer. Judge strictly and only from what is shown.

faithful: every number in the answer is supported by the results, allowing rounding and values derived from them (sums, differences, ratios, percentages).
responsive: the answer addresses the question that was asked, or clearly explains what part cannot be answered.
caveats_ok: if the answer relies on an approximation (for example a weighted average of block-group medians) or omits part of the question, it says so. True when no caveat was needed.
issues: short, specific strings; empty when none.
verdict: "pass" only if faithful and responsive are both true."""


class JudgeVerdict(BaseModel):
    faithful: bool
    responsive: bool
    caveats_ok: bool
    issues: list[str]
    verdict: Literal["pass", "fail"]


async def judge_answer(
    client: anthropic.AsyncAnthropic,
    model: str,
    question: str,
    answer: str,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = [
        {"purpose": q.get("purpose"), "columns": q.get("columns"), "rows": q.get("rows", [])[:30]} for q in queries
    ]
    prompt = (
        f"Question:\n{question}\n\nRetrieved results (JSON):\n{json.dumps(evidence, default=str)[:12000]}"
        f"\n\nAssistant answer:\n{answer}"
    )
    started = time.perf_counter()
    try:
        resp = await client.messages.parse(
            model=model,
            max_tokens=600,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=JudgeVerdict,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        verdict = resp.parsed_output
        if verdict is None:
            return {"verdict": "error", "issues": ["no parsed output"], "model": model, "elapsed_ms": elapsed_ms}
        return {**verdict.model_dump(), "model": model, "elapsed_ms": elapsed_ms}
    except Exception as exc:  # noqa: BLE001
        log.warning("judge failed: %s", str(exc)[:200])
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {"verdict": "error", "issues": [str(exc)[:120]], "model": model, "elapsed_ms": elapsed_ms}
