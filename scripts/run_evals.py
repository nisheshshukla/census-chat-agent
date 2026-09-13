"""Run the behavioral eval set and write docs/eval-results.md.

Modes:
  in-process (default): builds the agent from .env and runs turns directly.
  --url https://host      : drives a deployed instance over the SSE API (add --auth user:pass).

Scores behavior class per item, per-stage latency (p50/p95), and cache-hit rate on a second
pass of the answerable items (--second-pass).

Usage: .venv/bin/python scripts/run_evals.py [--url URL] [--auth U:P] [--only id,id] [--second-pass] [--judge]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "evals" / "questions.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
REPORT = ROOT / "docs" / "eval-results.md"


@dataclass
class Outcome:
    id: str
    cls: str
    question: str
    expected: str
    observed: str
    passed: bool
    reasons: list[str]
    elapsed_ms: int
    text: str
    queries: int
    error_kind: str | None
    trace: dict[str, Any] = field(default_factory=dict)
    queries_data: list[dict[str, Any]] = field(default_factory=list)
    judge: dict[str, Any] | None = None


def classify(final: dict[str, Any]) -> str:
    text: str = final.get("text", "")
    err = final.get("error_kind")
    nq = len(final.get("queries", []))
    if err == "guardrail_rejected":
        return "refused"
    if err:
        return "graceful"
    if nq == 0 and text.strip().endswith("?"):
        return "clarified"
    if nq == 0 and re.search(r"±|\d{1,3}(,\d{3})+", text):
        return "answered"
    if nq == 0:
        return "boundary"
    if re.search(
        r"\b(not (in|available)|isn't|aren't|can't|cannot|doesn't (include|cover)|no \d{4} (data|figures)|only (has|goes))\b",
        text,
        re.I,
    ) and not re.search(r"\d{2,}", text):
        return "boundary"
    return "answered"


def check(item: dict[str, Any], final: dict[str, Any], observed: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    text = final.get("text", "")
    expected_list = item["behavior"] if isinstance(item["behavior"], list) else [item["behavior"]]
    expected = "|".join(expected_list)
    states_limit = re.search(r"\b(not|no|isn't|aren't|can't|cannot|only|unavailable)\b", text, re.I)
    boundary_ok = "boundary" in expected_list and observed in ("answered", "clarified") and states_limit
    clarified_ok = "clarified" in expected_list and observed == "boundary" and "?" in text
    if observed in expected_list or boundary_ok or clarified_ok:
        pass
    else:
        reasons.append(f"behavior {observed} != {expected}")
    for pat in item.get("must_include", []):
        if not re.search(pat, text, re.I):
            reasons.append(f"missing /{pat}/")
    for pat in item.get("must_not_include", []):
        if re.search(pat, text, re.I):
            reasons.append(f"forbidden /{pat}/")
    num = item.get("number")
    if num:
        found = [float(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", text)]
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(million|billion|thousand)", text, re.I):
            scale = {"million": 1e6, "billion": 1e9, "thousand": 1e3}[m.group(2).lower()]
            found.append(float(m.group(1)) * scale)
        if not any(abs(f - num["value"]) / num["value"] <= num.get("tol", 0.01) for f in found):
            reasons.append(f"number {num['value']} not found (saw {found[:4]})")
    return not reasons, reasons


async def run_in_process(items: list[dict[str, Any]]) -> list[Outcome]:
    import anthropic

    from census_agent.agent.loop import run_turn
    from census_agent.app.main import build_deps
    from census_agent.app.sessions import SessionStore
    from census_agent.config import get_settings
    from census_agent.data.snowflake_client import SnowflakeClient

    settings = get_settings()
    sf = SnowflakeClient(settings)
    sf.warm()
    llm = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=1, timeout=45.0)
    deps = build_deps(settings, sf, llm)
    assert deps is not None
    store = SessionStore()
    sessions: dict[str, Any] = {}
    out: list[Outcome] = []
    for item in items:
        sid = item.get("session") or uuid.uuid4().hex
        session = sessions.setdefault(sid, store.get_or_create(None))
        q = item["question"] * item.get("repeat", 1)
        final: dict[str, Any] = {}
        trace: dict[str, Any] = {}
        t0 = time.perf_counter()
        async for ev in run_turn(deps, session, q, uuid.uuid4().hex[:8]):
            if ev["type"] == "final":
                final = ev
            elif ev["type"] == "trace":
                trace = ev["trace"]
        out.append(_outcome(item, final, trace, int((time.perf_counter() - t0) * 1000)))
        print(
            f"{'PASS' if out[-1].passed else 'FAIL'} {item['id']:28} {out[-1].observed:10} {out[-1].elapsed_ms:6}ms {'; '.join(out[-1].reasons)}"
        )
    return out


async def run_remote(items: list[dict[str, Any]], url: str, auth: str | None) -> list[Outcome]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    sessions: dict[str, str | None] = {}
    out: list[Outcome] = []
    async with httpx.AsyncClient(timeout=90.0, base_url=url.rstrip("/")) as client:
        for item in items:
            key = item.get("session") or uuid.uuid4().hex
            q = item["question"] * item.get("repeat", 1)
            final: dict[str, Any] = {}
            trace: dict[str, Any] = {}
            t0 = time.perf_counter()
            async with client.stream(
                "POST", "/api/chat", headers=headers, json={"message": q, "session_id": sessions.get(key)}
            ) as r:
                buf = ""
                async for chunk in r.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        for line in frame.split("\n"):
                            if line.startswith("data: "):
                                ev = json.loads(line[6:])
                                if ev["type"] == "session":
                                    sessions[key] = ev["session_id"]
                                elif ev["type"] == "final":
                                    final = ev
                                elif ev["type"] == "trace":
                                    trace = ev["trace"]
            out.append(_outcome(item, final, trace, int((time.perf_counter() - t0) * 1000)))
            print(
                f"{'PASS' if out[-1].passed else 'FAIL'} {item['id']:28} {out[-1].observed:10} {out[-1].elapsed_ms:6}ms {'; '.join(out[-1].reasons)}"
            )
    return out


def _outcome(item: dict[str, Any], final: dict[str, Any], trace: dict[str, Any], elapsed_ms: int) -> Outcome:
    observed = classify(final) if final else "graceful"
    passed, reasons = check(item, final, observed) if final else (False, ["no final event"])
    return Outcome(
        id=item["id"],
        cls=item["class"],
        question=item["question"][:80],
        expected="|".join(item["behavior"]) if isinstance(item["behavior"], list) else item["behavior"],
        observed=observed,
        passed=passed,
        reasons=reasons,
        elapsed_ms=elapsed_ms,
        text=final.get("text", ""),
        queries=len(final.get("queries", [])),
        error_kind=final.get("error_kind"),
        trace=trace,
        queries_data=final.get("queries", []),
    )


async def judge_outcomes(outcomes: list[Outcome], model: str) -> None:
    import anthropic

    from census_agent.config import get_settings
    from census_agent.guardrails.judge import judge_answer

    client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key, max_retries=1, timeout=60.0)
    sem = asyncio.Semaphore(4)

    async def one(o: Outcome) -> None:
        if not o.queries_data or o.error_kind:
            return
        async with sem:
            o.judge = await judge_answer(client, model, o.question, o.text, o.queries_data)
        print(f"JUDGE {o.id:28} {o.judge.get('verdict', '?'):6} {'; '.join(o.judge.get('issues', []))[:100]}")

    await asyncio.gather(*(one(o) for o in outcomes))


def pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round((len(s) - 1) * p)))]


def write_report(outcomes: list[Outcome], second: list[Outcome] | None, mode: str) -> None:
    by_class: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_class.setdefault(o.cls, []).append(o)
    lines = [
        "# Eval results",
        "",
        f"Run: {time.strftime('%Y-%m-%d %H:%M %Z')} · mode: {mode} · items: {len(outcomes)} · "
        f"pass: {sum(o.passed for o in outcomes)}/{len(outcomes)}",
        "",
        "## Pass rate by class",
        "",
        "| class | pass | items | p50 ms | p95 ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for cls, items in by_class.items():
        lat = [o.elapsed_ms for o in items]
        lines.append(f"| {cls} | {sum(o.passed for o in items)} | {len(items)} | {pct(lat, 0.5)} | {pct(lat, 0.95)} |")
    lat_all = [o.elapsed_ms for o in outcomes]
    lines += [
        "",
        f"Overall latency: p50 {pct(lat_all, 0.5)} ms · p95 {pct(lat_all, 0.95)} ms · max {max(lat_all) if lat_all else 0} ms",
        "",
    ]

    stage_lat: dict[str, list[int]] = {}
    for o in outcomes:
        for s in o.trace.get("stages", []):
            stage_lat.setdefault(s["name"], []).append(s["ms"])
    if stage_lat:
        lines += ["## Stage latency (ms)", "", "| stage | p50 | p95 |", "|---|---:|---:|"]
        for name, vals in stage_lat.items():
            lines.append(f"| {name} | {pct(vals, 0.5)} | {pct(vals, 0.95)} |")
        lines.append("")
    tok_in = [
        o.trace.get("tokens", {}).get("input", 0) + o.trace.get("tokens", {}).get("cache_read", 0)
        for o in outcomes
        if o.trace
    ]
    cache = [o.trace.get("tokens", {}).get("cache_read", 0) for o in outcomes if o.trace]
    ttft = [o.trace["first_token_ms"] for o in outcomes if o.trace and o.trace.get("first_token_ms")]
    if ttft:
        spec = sum(1 for o in outcomes if o.trace and o.trace.get("speculative"))
        lines.append(
            f"Time to first answer token: p50 {pct(ttft, 0.5)} ms · p95 {pct(ttft, 0.95)} ms · speculative queries used: {spec}"
        )
        lines.append("")
    if tok_in:
        lines.append(
            f"Prompt-cache read share: {sum(cache) / max(1, sum(tok_in)):.0%} of input tokens · median model calls: "
            f"{statistics.median(o.trace.get('model_calls', 0) for o in outcomes if o.trace):.0f}"
        )
        lines.append("")
    if second:
        hits = sum(1 for o in second for t in o.trace.get("tool_calls", []) if t.get("cached"))
        calls = sum(1 for o in second for t in o.trace.get("tool_calls", []) if t.get("name") == "run_census_sql")
        lat2 = [o.elapsed_ms for o in second]
        lines += [
            "## Second pass (cache)",
            "",
            f"SQL cache hits: {hits}/{calls} · p50 {pct(lat2, 0.5)} ms · p95 {pct(lat2, 0.95)} ms",
            "",
        ]

    judged = [o for o in outcomes if o.judge]
    if judged:
        passed = sum(1 for o in judged if o.judge and o.judge.get("verdict") == "pass")
        unfaithful = [o.id for o in judged if o.judge and o.judge.get("faithful") is False]
        lines += [
            "## LLM judge (answer faithfulness)",
            "",
            f"{passed}/{len(judged)} answered items pass: every figure supported by the retrieved rows, question addressed, "
            f"approximations labeled. Judge model: `{judged[0].trace.get('model', '')}`. "
            + (f"Unfaithful: {', '.join(unfaithful)}." if unfaithful else "No unfaithful answers."),
            "",
        ]
    lines += [
        "## Items",
        "",
        "| id | class | expected | observed | ms | result | judge |",
        "|---|---|---|---|---:|---|---|",
    ]
    for o in outcomes:
        res = "pass" if o.passed else "FAIL: " + "; ".join(o.reasons)
        j = (
            "—"
            if not o.judge
            else (
                o.judge.get("verdict", "?")
                + (": " + "; ".join(o.judge.get("issues", [])) if o.judge.get("issues") else "")
            )
        )
        lines.append(f"| {o.id} | {o.cls} | {o.expected} | {o.observed} | {o.elapsed_ms} | {res} | {j} |")
    lines += ["", "## Transcripts", ""]
    for o in outcomes:
        lines += [f"### {o.id}", "", f"**Q:** {o.question}", "", f"**A:** {o.text.strip()[:900]}", ""]
    REPORT.write_text("\n".join(lines))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
        json.dumps([o.__dict__ for o in outcomes], indent=1, default=str)
    )
    print(f"\nwrote {REPORT}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--auth")
    ap.add_argument("--only")
    ap.add_argument("--second-pass", action="store_true")
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge pass over answered items")
    args = ap.parse_args()
    items = yaml.safe_load(QUESTIONS.read_text())
    if args.only:
        wanted = set(args.only.split(","))
        items = [i for i in items if i["id"] in wanted]
    runner = (lambda it: run_remote(it, args.url, args.auth)) if args.url else run_in_process
    outcomes = await runner(items)
    if args.judge:
        from census_agent.config import get_settings

        print("\n--- LLM judge ---")
        await judge_outcomes(outcomes, get_settings().agent_model)
    second = None
    if args.second_pass:
        print("\n--- second pass (answerable, cache) ---")
        second = await runner([i for i in items if i["class"] == "answerable" and not i.get("session")])
    write_report(outcomes, second, "remote " + args.url if args.url else "in-process")
    print(f"\n{sum(o.passed for o in outcomes)}/{len(outcomes)} passed")


if __name__ == "__main__":
    asyncio.run(main())
