"""The agent turn: rules → pre-retrieval → streaming tool-use loop → grounding → session.

`run_turn` is an async generator of events. Every path, including every failure, ends with a
`final` event carrying user-facing text, so the client never sees a blank response.

Latency design: the topic classifier runs concurrently with the first model call rather than
before it. Its verdict is checked before any tool executes or any answer is finalized, so an
off-topic question is still rejected without touching Snowflake, but an on-topic question
never waits for the classifier.

Event types: session, status, token, reset, tool_call, tool_result, final, trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

import anthropic

from census_agent.agent.prompts import build_system_prompt
from census_agent.agent.tools import TOOL_DEFINITIONS, ToolRunner
from census_agent.agent.trace import TurnTrace
from census_agent.app.sessions import Session, TurnRecord
from census_agent.config import Settings
from census_agent.data.catalog import Catalog
from census_agent.data.concepts import curated_matches
from census_agent.data.field_search import FieldSearch
from census_agent.data.geography import GeographyResolver
from census_agent.errors import (
    USER_MESSAGES,
    AgentError,
    BudgetExhaustedError,
    DataUnavailableError,
    ErrorKind,
    LLMUnavailableError,
    classify_exception,
)
from census_agent.guardrails.classifier import Classifier, Verdict
from census_agent.guardrails.grounding import caveat_for, check_grounding
from census_agent.guardrails.rules import check_input
from census_agent.logging_setup import log_event

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_SQL_RETRIES = 2
MIN_SECONDS_FOR_ANOTHER_CALL = 8.0
GROUNDING_PRIOR_TURNS = 2
FOLLOWUP_LINE_RE = re.compile(r"^\s*\**\s*Follow-ups?\s*:?\s*\**\s*(.+)$", re.I)
SIMULATIONS = {
    "snowflake-down": "data_unavailable",
    "data-unavailable": "data_unavailable",
    "llm-down": "llm_unavailable",
    "budget": "budget_exhausted",
    "ungrounded": "ungrounded",
}


def split_followups(text: str) -> tuple[str, list[str]]:
    """Strip the trailing 'Follow-ups: a | b | c' line the prompt asks for; return (answer, suggestions)."""
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
        m = FOLLOWUP_LINE_RE.match(lines[i])
        if m:
            items = [x.strip(" *-•\"'") for x in m.group(1).split("|")]
            items = [x for x in items if 8 <= len(x) <= 140][:3]
            answer = "\n".join(lines[:i]).rstrip()
            return (answer or text.strip(), items)
    return text.strip(), []


def parse_simulation(question: str, enabled: bool) -> tuple[str | None, str]:
    """`/simulate snowflake-down What is ...` -> ("data_unavailable", "What is ...")."""
    if not enabled or not question.lstrip().lower().startswith("/simulate"):
        return None, question
    parts = question.strip().split(None, 2)
    mode = SIMULATIONS.get(parts[1].lower()) if len(parts) > 1 else None
    rest = parts[2] if len(parts) > 2 else "What is the population of Texas?"
    return (mode or "unknown"), rest


META_ANSWER = (
    "I answer questions about the US population using Census data (ACS 2016-2020, ACS 2015-2019, "
    "and the 2020 Decennial Census) at state, county, tract, and block-group level. Try: "
    "'What is the population of Cook County, IL?', 'Which Texas counties have the most residents?', "
    "or 'What share of Florida's population is 65 or older?'"
)


@dataclass
class AgentDeps:
    settings: Settings
    catalog: Catalog
    field_search: FieldSearch
    resolver: GeographyResolver
    snowflake: Any
    llm: anthropic.AsyncAnthropic
    classifier: Classifier | None
    sql_cache: Any
    system_prompt: str

    @classmethod
    def build(
        cls,
        settings: Settings,
        catalog: Catalog,
        field_search: FieldSearch,
        resolver: GeographyResolver,
        snowflake: Any,
        llm: anthropic.AsyncAnthropic,
        sql_cache: Any,
    ) -> AgentDeps:
        classifier = Classifier(llm, settings.classifier_model) if settings.classifier_enabled else None
        return cls(
            settings,
            catalog,
            field_search,
            resolver,
            snowflake,
            llm,
            classifier,
            sql_cache,
            build_system_prompt(catalog.database),
        )


def _event(type_: str, **data: Any) -> dict[str, Any]:
    return {"type": type_, **data}


@dataclass
class PreRetrieval:
    context_text: str
    geo_hits: list[dict[str, Any]]
    field_hits: list[dict[str, Any]]
    classifier_task: asyncio.Task[Verdict | None] | None


async def _preretrieve(
    deps: AgentDeps, question: str, session: Session, trace: TurnTrace, runner: ToolRunner
) -> PreRetrieval:
    """Field search and geography scan (fast, local); the classifier is started, not awaited."""
    classifier_task: asyncio.Task[Verdict | None] | None = None
    if deps.classifier is not None:
        ctx = session.recent_context_text() + "\n" + session.card.render()
        classifier_task = asyncio.create_task(deps.classifier.classify(question, ctx))

    async def fields() -> list[dict[str, Any]]:
        try:
            hits, backend = await asyncio.wait_for(deps.field_search.search(question, k=8), timeout=1.5)
            trace.search_backend = backend
            return [h.to_dict() for h in hits]
        except Exception as exc:  # noqa: BLE001
            log.warning("pre-retrieval field search failed: %s", exc)
            return []

    def geos() -> list[dict[str, Any]]:
        try:
            return [r.to_dict() for r in deps.resolver.find_places(question)]
        except Exception as exc:  # noqa: BLE001
            log.warning("pre-retrieval geography failed: %s", exc)
            return []

    field_hits, geo_hits = await asyncio.gather(fields(), asyncio.to_thread(geos))
    keep = ("column", "table", "title", "path", "universe", "summable", "moe_column")
    ctx_obj: dict[str, Any] = {
        "context_card": session.card.render(),
        "resolved_geographies": geo_hits,
        "candidate_fields": [{k: v for k, v in f.items() if k in keep} for f in field_hits],
    }
    spec = await _speculate(deps, runner, question, geo_hits, trace)
    if spec is not None:
        ctx_obj["speculative_result"] = spec
    text = "CONTEXT (pre-retrieved, may be incomplete):\n" + json.dumps(ctx_obj, ensure_ascii=False)
    return PreRetrieval(text, geo_hits, field_hits, classifier_task)


async def _speculate(
    deps: AgentDeps, runner: ToolRunner, question: str, geo_hits: list[dict[str, Any]], trace: TurnTrace
) -> dict[str, Any] | None:
    """Run the obvious query before the first model call for the simplest question shape:
    exactly one resolved geography and one curated, summable measure. Saves a full model
    round trip (the model answers in one call) and cuts time-to-first-token roughly in half."""
    if not deps.settings.speculative_execution:
        return None
    resolved = [g for g in geo_hits if g.get("status") == "resolved" and g.get("matches")]
    if len(resolved) != 1 or len(geo_hits) != 1:
        return None
    concepts = curated_matches(question)
    if not concepts or concepts[0][0] == "SEMANTIC":
        return None
    table, column = concepts[0]
    info = deps.catalog.field(table, column)
    if info is None or not info.is_summable or info.kind not in ("estimate", "count"):
        return None
    geo = resolved[0]["matches"][0]
    share = deps.catalog.database
    moe = info.moe_column
    select = f'SUM(t."{column}") AS {_slug(info.path or info.table_title)}'
    if (
        moe
        and deps.catalog.table_columns("PUBLIC", table)
        and moe in (deps.catalog.table_columns("PUBLIC", table) or {})
    ):
        select += f', SQRT(SUM(POWER(t."{moe}", 2))) AS moe'
    if geo["level"] == "nation":
        sql = f'SELECT {select} FROM {share}.PUBLIC."{table}" t'
    else:
        where = f"g.STATE_FIPS = '{geo['state_fips']}'"
        if geo["level"] == "county":
            where += f" AND g.COUNTY_FIPS = '{geo['county_fips']}'"
        sql = (
            f'SELECT {select} FROM {share}.PUBLIC."{table}" t '
            f"JOIN CENSUS_APP_DB.SEMANTIC.GEO_CBG g ON g.CENSUS_BLOCK_GROUP = t.CENSUS_BLOCK_GROUP WHERE {where}"
        )
    purpose = f"{info.table_title} for {geo['label']} (speculative)"
    try:
        content, is_error = await asyncio.wait_for(
            runner.run("run_census_sql", {"sql": sql, "purpose": purpose}, trace), timeout=4.0
        )
    except Exception as exc:  # noqa: BLE001
        log.info("speculative query skipped: %s", exc)
        return None
    if is_error:
        return None
    trace.speculative = True
    result = json.loads(content)
    return {
        "measure": info.label(),
        "column": column,
        "table": table,
        "vintage": info.vintage,
        "geography": geo["label"],
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "note": (
            "Pre-run for the most likely reading of the question. Use it only if it answers "
            "exactly what was asked; otherwise ignore it and query yourself."
        ),
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "value"


async def _verdict(task: asyncio.Task[Verdict | None] | None) -> Verdict | None:
    if task is None:
        return None
    try:
        return await task
    except Exception:  # noqa: BLE001
        return None


def _fast_fail_text(verdict: Verdict) -> str:
    if verdict.category == "off_topic" and "meta" in verdict.reason.lower():
        return META_ANSWER
    return USER_MESSAGES[ErrorKind.GUARDRAIL_REJECTED]


async def run_turn(
    deps: AgentDeps, session: Session, question: str, request_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    settings = deps.settings
    trace = TurnTrace(request_id=request_id, session_id=session.id, model=settings.agent_model)
    runner = ToolRunner(settings, deps.catalog, deps.field_search, deps.resolver, deps.snowflake, deps.sql_cache)
    deadline = time.perf_counter() + settings.turn_wall_clock_s
    trace.config = {
        "effort": settings.agent_effort,
        "thinking": "adaptive (always on; sampling parameters are not configurable on this model)",
        "max_output_tokens": 4000,
        "wall_clock_s": settings.turn_wall_clock_s,
        "max_tool_calls": settings.max_tool_calls,
        "statement_timeout_s": settings.snowflake_statement_timeout_s,
        "max_result_rows": settings.max_result_rows,
        "classifier_model": settings.classifier_model if settings.classifier_enabled else None,
        "grounding": settings.grounding_enabled,
        "speculative_execution": settings.speculative_execution,
    }
    yield _event("session", session_id=session.id, request_id=request_id)

    simulate, question = parse_simulation(question, settings.simulate_failures_enabled)
    if simulate == "unknown":
        text = (
            "Unknown simulation. Try: /simulate snowflake-down, /simulate llm-down, "
            "/simulate budget, /simulate ungrounded."
        )
        yield _event("final", text=text, error_kind=None, queries=[], grounding_ok=True, suggestions=[])
        yield _event("trace", trace=trace.to_dict())
        return
    if simulate:
        trace.config["simulation"] = simulate
        if simulate == "budget_exhausted":
            deadline = time.perf_counter()

    rule = check_input(question, settings.max_input_chars)
    if rule.blocks:
        trace.guardrail = f"rule:{rule.kind}"
        text = {
            "empty": "Please type a question about the US population, for example: what is the population of Texas?",
            "too_long": f"That message is too long (limit {settings.max_input_chars} characters). Please shorten it.",
            "injection": USER_MESSAGES[ErrorKind.GUARDRAIL_REJECTED],
        }[rule.kind]
        trace.stage("rules")
        yield _event("final", text=text, error_kind="guardrail_rejected", queries=[], grounding_ok=True, suggestions=[])
        _record(session, question, text, [], [], trace, "guardrail_rejected")
        yield _event("trace", trace=trace.to_dict())
        return
    trace.stage("rules")

    yield _event("status", text="Understanding the question")
    try:
        if simulate and simulate != "ungrounded":
            pre = PreRetrieval("CONTEXT: unavailable (simulated failure)", [], [], None)
        else:
            pre = await _preretrieve(deps, question, session, trace, runner)
    except Exception as exc:  # noqa: BLE001
        log.exception("pre-retrieval crashed: %s", exc)
        pre = PreRetrieval("CONTEXT: unavailable", [], [], None)
    trace.stage("preretrieval")

    user_content = [{"type": "text", "text": question}, {"type": "text", "text": pre.context_text}]
    new_messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    messages = session.history_messages() + new_messages
    answer_text = ""
    error_kind: str | None = None
    failed_sql: dict[str, int] = {}
    verdict_checked = False
    streamed_any = False

    try:
        if simulate == "data_unavailable":
            raise DataUnavailableError("simulated: snowflake unreachable")
        if simulate == "llm_unavailable":
            raise LLMUnavailableError("simulated: model unavailable")
        for _ in range(settings.max_tool_calls + 2):
            remaining = deadline - time.perf_counter()
            if remaining < MIN_SECONDS_FOR_ANOTHER_CALL:
                raise BudgetExhaustedError(f"{settings.turn_wall_clock_s}s wall clock")
            tools_exhausted = len(trace.tool_calls) >= settings.max_tool_calls and trace.model_calls > 0
            tools: list[dict[str, Any]] = [] if tools_exhausted else TOOL_DEFINITIONS
            if tools_exhausted:
                yield _event("status", text="Writing the answer")
            else:
                yield _event("status", text="Thinking" if trace.model_calls == 0 else "Analyzing results")

            trace.model_calls += 1
            text_buf = ""
            tool_uses: list[tuple[str, str, dict[str, Any]]] = []
            t_call = time.perf_counter()
            first_event_seen = False
            first_text_at: float | None = None
            async with deps.llm.beta.messages.stream(
                model=settings.agent_model,
                max_tokens=4000,
                system=[{"type": "text", "text": deps.system_prompt, "cache_control": {"type": "ephemeral"}}],
                tools=cast(Any, tools),
                messages=cast(Any, messages),
                output_config={"effort": settings.agent_effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                timeout=max(10.0, remaining - 1.0),
            ) as stream:
                async for ev in stream:
                    if not first_event_seen:
                        first_event_seen = True
                        trace.model_ttft_ms.append(int((time.perf_counter() - t_call) * 1000))
                    if ev.type == "text":
                        if first_text_at is None:
                            first_text_at = time.perf_counter()
                        text_buf += ev.text
                        streamed_any = True
                        yield _event("token", text=ev.text)
                final_message = await stream.get_final_message()
            trace.add_usage(final_message.usage)
            if final_message.model != settings.agent_model:
                trace.fallback_model = final_message.model

            if not verdict_checked:
                verdict_checked = True
                verdict = await _verdict(pre.classifier_task)
                trace.classifier_category = verdict.category if verdict else None
                established = any(t.error_kind is None and t.queries for t in session.turns)
                on_topic_signals = bool(pre.geo_hits) or bool(curated_matches(question))
                if verdict and verdict.category == "off_topic" and (established or on_topic_signals):
                    log.info("classifier off_topic ignored in an established session; model decides")
                    verdict = None
                if verdict and verdict.category in ("off_topic", "adversarial"):
                    trace.guardrail = f"classifier:{verdict.category}"
                    if streamed_any:
                        yield _event("reset")
                    answer_text = _fast_fail_text(verdict)
                    error_kind = "guardrail_rejected"
                    break

            if final_message.stop_reason == "refusal":
                answer_text = (
                    "I can't help with that request. I can answer questions about US population "
                    "and demographics from the Census dataset."
                )
                error_kind = "guardrail_rejected"
                trace.guardrail = "model:refusal"
                break

            for block in final_message.content:
                if block.type == "tool_use" and not tools_exhausted:
                    tool_uses.append((block.id, block.name, dict(block.input)))
            new_messages.append(
                {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in final_message.content]}
            )
            messages = session.history_messages() + new_messages

            if final_message.stop_reason != "tool_use" or not tool_uses:
                if first_text_at is not None:
                    trace.first_token_ms = int((first_text_at - trace.started) * 1000)
                answer_text = text_buf.strip()
                if final_message.stop_reason == "max_tokens":
                    answer_text += "\n\n*(answer truncated)*"
                break

            if text_buf.strip():
                yield _event("reset")
            for tid, name, args in tool_uses:
                yield _event("tool_call", id=tid, name=name, args=args)
                yield _event("status", text=_status_for(name, args))

            results = await runner.run_many(tool_uses, trace)
            tool_results = []
            for (tid, name, args), (_, content, is_error) in zip(tool_uses, results, strict=True):
                if name == "run_census_sql" and is_error:
                    key = args.get("sql", "").strip()
                    failed_sql[key] = failed_sql.get(key, 0) + 1
                    if failed_sql[key] > MAX_SQL_RETRIES:
                        content += (
                            " This exact query has failed repeatedly; do not retry it. "
                            "Explain the problem to the user instead."
                        )
                yield _event("tool_result", id=tid, name=name, ok=not is_error, preview=content[:400])
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}
                )
            new_messages.append({"role": "user", "content": tool_results})
            messages = session.history_messages() + new_messages
        else:
            raise BudgetExhaustedError("tool-call budget")
    except AgentError as exc:
        error_kind = exc.kind.value
        answer_text = exc.user_message
        log.warning("turn ended with %s: %s", exc.kind, exc.detail)
    except anthropic.APITimeoutError as exc:
        error_kind = ErrorKind.BUDGET_EXHAUSTED.value
        answer_text = USER_MESSAGES[ErrorKind.BUDGET_EXHAUSTED]
        log.warning("llm timeout: %s", exc)
    except anthropic.APIError as exc:
        error_kind = ErrorKind.LLM_UNAVAILABLE.value
        answer_text = LLMUnavailableError().user_message
        log.error("llm error %s: %s", type(exc).__name__, str(exc)[:300])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        kind = classify_exception(exc)
        error_kind = kind.value
        answer_text = USER_MESSAGES[kind]
        log.exception("turn crashed: %s", exc)
    finally:
        if pre.classifier_task and not pre.classifier_task.done():
            pre.classifier_task.cancel()
    trace.stage("model_loop")
    trace.error_kind = error_kind

    suggestions: list[str] = []
    if error_kind is None and answer_text:
        answer_text, suggestions = split_followups(answer_text)
    if simulate == "ungrounded" and error_kind is None:
        answer_text += "\n\nRoughly 12,345,678 of them live in rented homes."
    queries = [q.to_dict() for q in runner.executed]
    grounding_ok = True
    if settings.grounding_enabled and error_kind is None and answer_text:
        evidence = [{"rows": q.rows} for q in runner.executed]
        for turn in session.turns[-GROUNDING_PRIOR_TURNS:]:
            evidence.extend({"rows": q.get("rows", [])} for q in turn.queries)
        report = check_grounding(answer_text, evidence)
        trace.grounding_ungrounded = report.ungrounded
        grounding_ok = report.ok
        if not report.ok:
            answer_text += caveat_for(report)
            log_event(log, "grounding flagged", ungrounded=report.ungrounded)
    trace.stage("grounding")

    if not answer_text:
        answer_text = USER_MESSAGES[ErrorKind.INTERNAL]
        error_kind = error_kind or ErrorKind.INTERNAL.value

    yield _event(
        "final",
        text=answer_text,
        error_kind=error_kind,
        queries=queries,
        grounding_ok=grounding_ok,
        suggestions=suggestions,
        turn_index=len(session.turns),
    )
    stored = new_messages if error_kind is None else _plain_turn(question, answer_text)
    _record(session, question, answer_text, stored, queries, trace, error_kind, runner, pre.geo_hits, suggestions)
    log_event(
        log,
        "turn",
        elapsed_ms=trace.elapsed_ms,
        model_calls=trace.model_calls,
        tool_calls=len(trace.tool_calls),
        tokens_in=trace.input_tokens,
        tokens_out=trace.output_tokens,
        cache_read=trace.cache_read_tokens,
        error_kind=error_kind,
        classifier=trace.classifier_category,
        grounding_ok=grounding_ok,
    )
    yield _event("trace", trace=trace.to_dict())


def _status_for(name: str, args: dict[str, Any]) -> str:
    if name == "run_census_sql":
        return "Running query" + (f": {args['purpose'][:80]}" if args.get("purpose") else "")
    if name == "search_census_fields":
        return f"Finding fields for “{args.get('query', '')[:60]}”"
    if name == "describe_table":
        return f"Reading table {args.get('table_name', '')}"
    if name == "resolve_geography":
        return f"Resolving “{args.get('name', '')[:60]}”"
    return name


def _plain_turn(question: str, answer: str) -> list[dict[str, Any]]:
    """History entry for a failed turn: keep the exchange, drop partial tool blocks."""
    return [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]


def _record(
    session: Session,
    question: str,
    answer: str,
    messages: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    trace: TurnTrace,
    error_kind: str | None,
    runner: ToolRunner | None = None,
    geo_hits: list[dict[str, Any]] | None = None,
    suggestions: list[str] | None = None,
) -> None:
    if messages and messages[-1]["role"] != "assistant":
        messages = messages + [{"role": "assistant", "content": answer or "(no answer)"}]
    if not messages:
        messages = _plain_turn(question, answer)
    session.turns.append(
        TurnRecord(
            question=question,
            answer=answer,
            messages=messages,
            queries=queries,
            trace=trace.to_dict(),
            error_kind=error_kind,
            suggestions=suggestions or [],
        )
    )
    if error_kind is None and runner is not None:
        card = session.card
        resolved = [
            g for g in (geo_hits or []) + runner.geographies if g.get("status") == "resolved" and g.get("matches")
        ]
        labels = [g["matches"][0]["label"] for g in resolved]
        if labels:
            card.last_geographies = list(dict.fromkeys(card.last_geographies + labels))[-6:]
        if runner.executed:
            last = runner.executed[-1]
            card.last_sql = last.sql
            card.last_result_preview = json.dumps({"columns": last.columns, "first_rows": last.rows[:3]}, default=str)
            cols = [c for q in runner.executed for c in _columns_in_sql(q.sql)]
            if cols:
                card.last_measures = list(dict.fromkeys(card.last_measures + cols))[-6:]
            card.last_vintage = "2019" if '"2019_CBG' in last.sql and '"2020_CBG' not in last.sql else "2020"
    session.touch()


_COLUMN_RE = re.compile(r'"([A-Z]\d{5}[A-Z]{0,2}e\d+|P0\d{6}|H0\d{6})"')


def _columns_in_sql(sql: str) -> list[str]:
    return list(dict.fromkeys(_COLUMN_RE.findall(sql)))
