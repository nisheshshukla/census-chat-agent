"""Developer CLI. `census-agent ask "..."` runs the same turn the API runs, printing events."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid


def _cmd_health(_: argparse.Namespace) -> int:
    from census_agent.config import get_settings
    from census_agent.data.snowflake_client import SnowflakeClient

    settings = get_settings()
    print(
        json.dumps(
            {
                "anthropic_configured": settings.anthropic_configured,
                "snowflake_configured": settings.snowflake_configured,
            }
        )
    )
    if settings.snowflake_configured:
        client = SnowflakeClient(settings)
        ok = asyncio.run(client.ping())
        print(json.dumps({"snowflake_ping": ok}))
        return 0 if ok else 1
    return 1


async def _ask(questions: list[str], show_trace: bool) -> int:
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
    session = SessionStore().get_or_create(None)
    for q in questions:
        print(f"\n>>> {q}")
        answer_started = False
        async for ev in run_turn(deps, session, q, uuid.uuid4().hex[:8]):
            t = ev["type"]
            if t == "status":
                print(f"  [{ev['text']}]", file=sys.stderr)
            elif t == "tool_call":
                a = ev["args"]
                brief = a.get("sql", a.get("query", a.get("name", a.get("table_name", ""))))
                print(f"  -> {ev['name']}: {str(brief)[:300]}", file=sys.stderr)
            elif t == "tool_result":
                print(
                    f"  <- {ev['name']} {'ok' if ev['ok'] else 'ERROR'}: {ev['preview'][:200]}",
                    file=sys.stderr,
                )
            elif t == "reset":
                answer_started = False
            elif t == "token":
                if not answer_started:
                    print("\n", end="")
                    answer_started = True
                print(ev["text"], end="", flush=True)
            elif t == "final":
                print("\n\n--- final ---")
                print(ev["text"])
                if ev.get("error_kind"):
                    print(f"[error_kind={ev['error_kind']}]")
            elif t == "trace" and show_trace:
                print(json.dumps(ev["trace"], indent=1))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    return asyncio.run(_ask(args.question, args.trace))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="census-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health").set_defaults(fn=_cmd_health)
    ask = sub.add_parser("ask", help="ask one or more questions in one session")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--trace", action="store_true")
    ask.set_defaults(fn=_cmd_ask)
    ns = parser.parse_args(argv)
    return int(ns.fn(ns))


if __name__ == "__main__":
    raise SystemExit(main())
