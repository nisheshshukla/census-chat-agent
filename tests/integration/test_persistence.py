from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fastapi.testclient import TestClient

from census_agent.app.main import create_app
from census_agent.app.persistence import SessionRepository, session_from_dict, session_to_dict
from census_agent.app.sessions import ContextCard, Session, TurnRecord
from census_agent.config import Settings
from census_agent.data.snowflake_client import QueryResult
from tests.integration.test_chat_api import SQL_CA, FakeLLM, _events


class PersistingFakeSnowflake:
    """Answers census queries like the chat fake and implements the SESSIONS table in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.statements: list[str] = []

    async def query(self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int | None = None) -> QueryResult:
        self.statements.append(sql)
        head = sql.lstrip()[:12].upper()
        if head.startswith("MERGE INTO"):
            assert params is not None
            self.rows[str(params[0])] = str(params[1])
            return QueryResult(columns=["rows"], rows=[(1,)], elapsed_ms=1)
        if head.startswith("SELECT TO_JS"):
            assert params is not None
            payload = self.rows.get(str(params[0]))
            return QueryResult(columns=["P"], rows=[(payload,)] if payload else [], elapsed_ms=1)
        if head.startswith(("CREATE TABLE", "DELETE FROM")):
            if "SESSION_ID = %s" in sql and params:
                self.rows.pop(str(params[0]), None)
            return QueryResult(columns=[], rows=[], elapsed_ms=1)
        return QueryResult(columns=["POPULATION"], rows=[(39346023.0,)], elapsed_ms=1)

    async def ping(self) -> bool:
        return True


def _settings() -> Settings:
    return Settings(
        _env_file=None, anthropic_api_key="t", snowflake_account="X-Y", demo_password="", speculative_execution=False
    )


def test_session_round_trips_through_json() -> None:
    s = Session(id="abc123abc123")
    s.card = ContextCard(last_geographies=["Texas"], last_measures=["B01003e1"], last_sql="SELECT 1")
    s.turns.append(
        TurnRecord(question="q", answer="a", messages=[{"role": "user", "content": "q"}], suggestions=["x?"])
    )
    back = session_from_dict(json.loads(json.dumps(session_to_dict(s))))
    assert back.id == s.id and back.card.last_geographies == ["Texas"] and back.turns[0].suggestions == ["x?"]
    assert back.turns[0].messages == s.turns[0].messages


def test_conversation_survives_a_process_restart() -> None:
    db = PersistingFakeSnowflake()
    llm = FakeLLM(
        [
            {
                "stop_reason": "tool_use",
                "tool_uses": [{"id": "t1", "name": "run_census_sql", "input": {"sql": SQL_CA, "purpose": "CA"}}],
            },
            {"text_chunks": ["California has 39,346,023 people."]},
        ]
    )
    with TestClient(create_app(_settings(), snowflake=db, llm=llm)) as client:  # type: ignore[arg-type]
        events = _events(client, "Population of California?")
        sid = events[0]["session_id"]
        assert client.get("/api/health").json()["session_persistence"] is True
    assert sid in db.rows, "turn was persisted after the response"

    with TestClient(create_app(_settings(), snowflake=db, llm=FakeLLM([]))) as fresh:  # type: ignore[arg-type]
        restored = fresh.get(f"/api/session/{sid}").json()
        assert len(restored["turns"]) == 1
        assert "39,346,023" in restored["turns"][0]["answer"]
        assert restored["card"]["last_measures"] == ["B01003e1"]
        fresh.post(f"/api/session/{sid}/reset")
        assert fresh.get(f"/api/session/{sid}").json()["turns"] == []
    assert sid not in db.rows


async def test_repository_failures_never_raise() -> None:
    class Broken:
        async def query(self, *a: Any, **k: Any) -> QueryResult:
            raise RuntimeError("down")

        async def ping(self) -> bool:
            return False

    repo = SessionRepository(Broken())  # type: ignore[arg-type]
    assert await repo.prepare() is False
    await repo.save(Session(id="abcdefabcdef"))
    assert await repo.load("abcdefabcdef") is None
