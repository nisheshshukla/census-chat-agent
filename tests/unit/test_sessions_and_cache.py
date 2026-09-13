from __future__ import annotations

import time

from census_agent.agent.cache import TTLCache, normalize_sql
from census_agent.app.sessions import MAX_TURNS_VERBATIM, ContextCard, Session, SessionStore, TurnRecord


def _turn(i: int, with_tools: bool = True) -> TurnRecord:
    msgs: list[dict] = [{"role": "user", "content": f"q{i}"}]
    if with_tools:
        msgs.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "run_census_sql", "input": {}}],
            }
        )
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "{}"}]})
    msgs.append({"role": "assistant", "content": f"a{i}"})
    return TurnRecord(question=f"q{i}", answer=f"a{i}", messages=msgs)


def test_history_window_keeps_recent_turns_verbatim_and_summarizes_older() -> None:
    s = Session(id="abc")
    for i in range(6):
        s.turns.append(_turn(i))
    hist = s.history_messages()
    # Older turns collapse into one summary exchange (2 messages), recent ones stay verbatim (4 each).
    assert hist[0]["role"] == "user" and "Earlier in this conversation" in hist[0]["content"]
    assert "q0" in hist[0]["content"] and "a2" in hist[0]["content"]
    assert len(hist) == 2 + MAX_TURNS_VERBATIM * 4
    assert hist[-1]["content"] == "a5"


def test_history_is_capped_by_characters() -> None:
    s = Session(id="abc")
    big = "x" * 20000
    for i in range(3):
        t = _turn(i, with_tools=False)
        t.messages[-1]["content"] = big
        s.turns.append(t)
    hist = s.history_messages()
    assert sum(len(str(m["content"])) for m in hist) < 30000
    assert hist[-1]["content"] == big  # newest survives


def test_context_card_render_and_first_turn() -> None:
    c = ContextCard()
    assert "first turn" in c.render()
    c.last_geographies = ["California"]
    c.last_measures = ["B01003e1"]
    c.last_sql = "SELECT 1"
    r = c.render()
    assert "California" in r and "B01003e1" in r and "SELECT 1" in r


def test_session_store_ttl_and_reset() -> None:
    store = SessionStore(ttl_s=0.05)
    s = store.get_or_create(None)
    assert len(s.id) == 32 and store.get(s.id) is s
    s2 = store.get_or_create("my-session-id")
    assert s2.id == "my-session-id"
    time.sleep(0.06)
    assert store.get(s.id) is None
    s3 = store.reset("my-session-id")
    assert s3.id == "my-session-id" and s3.turns == []


def test_ttl_cache() -> None:
    c: TTLCache[int] = TTLCache(max_items=2, ttl_s=0.05)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)  # evicts a
    assert c.get("a") is None and c.get("b") == 2 and c.get("c") == 3
    time.sleep(0.06)
    assert c.get("b") is None
    assert c.hits == 2 and c.misses == 2


def test_normalize_sql() -> None:
    assert normalize_sql("SELECT  1\n FROM x ;") == normalize_sql("select 1 from x")
