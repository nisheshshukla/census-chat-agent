from __future__ import annotations

import json
from typing import Any

from census_agent import cli
from census_agent.config import Settings


def test_health_command(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, anthropic_api_key="t", snowflake_account="X-Y"),
        raising=False,
    )

    class FakeClient:
        def __init__(self, settings: Any) -> None:
            pass

        async def ping(self) -> bool:
            return True

    import census_agent.data.snowflake_client as m

    monkeypatch.setattr(m, "SnowflakeClient", FakeClient)
    monkeypatch.setattr(
        "census_agent.config.get_settings",
        lambda: Settings(_env_file=None, anthropic_api_key="t", snowflake_account="X-Y"),
    )
    assert cli.main(["health"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["snowflake_configured"] is True and lines[1]["snowflake_ping"] is True


def test_ask_command_prints_events(monkeypatch: Any, capsys: Any) -> None:
    async def fake_run_turn(deps: Any, session: Any, q: str, rid: str):  # noqa: ANN202
        yield {"type": "session", "session_id": "s", "request_id": rid}
        yield {"type": "status", "text": "Thinking"}
        yield {"type": "tool_call", "id": "t", "name": "run_census_sql", "args": {"sql": "SELECT 1"}}
        yield {"type": "tool_result", "id": "t", "name": "run_census_sql", "ok": True, "preview": "{}"}
        yield {"type": "token", "text": "Texas has "}
        yield {"type": "reset"}
        yield {"type": "token", "text": "28,635,442 people."}
        yield {
            "type": "final",
            "text": "Texas has 28,635,442 people.",
            "error_kind": None,
            "queries": [],
            "grounding_ok": True,
        }
        yield {"type": "trace", "trace": {"elapsed_ms": 1}}

    class FakeSF:
        def __init__(self, settings: Any) -> None:
            pass

        def warm(self) -> None:
            pass

    monkeypatch.setattr(
        "census_agent.config.get_settings",
        lambda: Settings(_env_file=None, anthropic_api_key="t", snowflake_account="X-Y"),
    )
    monkeypatch.setattr("census_agent.data.snowflake_client.SnowflakeClient", FakeSF)
    monkeypatch.setattr("census_agent.app.main.build_deps", lambda s, sf, llm: object())
    monkeypatch.setattr("census_agent.agent.loop.run_turn", fake_run_turn)
    assert cli.main(["ask", "population of texas", "--trace"]) == 0
    out = capsys.readouterr()
    assert "--- final ---" in out.out and "28,635,442" in out.out and '"elapsed_ms": 1' in out.out
    assert "-> run_census_sql" in out.err
