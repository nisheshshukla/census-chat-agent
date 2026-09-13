from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from census_agent.app.main import create_app
from census_agent.app.security import RateLimiter
from census_agent.config import Settings
from tests.integration.test_chat_api import FakeLLM, FakeSnowflake


def _app(password: str = "s3cret"):  # noqa: ANN202
    settings = Settings(
        _env_file=None, anthropic_api_key="t", snowflake_account="X-Y", demo_password=password, demo_user="reviewer"
    )
    return create_app(settings, snowflake=FakeSnowflake(), llm=FakeLLM([{"text_chunks": ["hi"]}] * 20))  # type: ignore[arg-type]


def _auth(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_health_and_page_are_open_but_api_requires_auth() -> None:
    with TestClient(_app()) as client:
        assert client.get("/api/health").status_code == 200
        r = client.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 401 and "www-authenticate" not in r.headers  # SPA sign-in, not a browser dialog
        assert client.post("/api/chat", json={"message": "hi"}, headers=_auth("reviewer", "wrong")).status_code == 401
        assert client.post("/api/chat", json={"message": "hi"}, headers=_auth("reviewer", "s3cret")).status_code == 200


def test_login_sets_cookie_that_authorizes_api() -> None:
    with TestClient(_app()) as client:
        assert client.get("/api/me").status_code == 401
        bad = client.post("/api/login", json={"password": "nope"})
        assert bad.status_code == 401 and bad.json()["error"] == "invalid_password"
        ok = client.post("/api/login", json={"password": " s3cret "})  # whitespace tolerated
        assert ok.status_code == 200 and "census_session" in ok.cookies
        assert client.get("/api/me").status_code == 200
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200


def test_session_token_expiry_and_tampering() -> None:
    from census_agent.app.security import make_session_token, verify_session_token
    from census_agent.config import Settings

    s = Settings(_env_file=None, demo_password="pw")
    tok = make_session_token(s)
    assert verify_session_token(s, tok)
    flipped = tok[:-1] + ("1" if tok[-1] == "0" else "0")
    assert not verify_session_token(s, flipped)
    assert not verify_session_token(s, make_session_token(s, now=0))  # expired long ago
    assert not verify_session_token(Settings(_env_file=None, demo_password="other"), tok)


def test_auth_disabled_when_no_password() -> None:
    with TestClient(_app(password="")) as client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200


def test_rate_limiter_per_session_and_global() -> None:
    rl = RateLimiter(per_session=2, global_limit=3, window_s=60)
    assert rl.allow("a") == (True, "")
    assert rl.allow("a") == (True, "")
    ok, why = rl.allow("a")
    assert not ok and "lot of questions" in why
    assert rl.allow("b")[0] is True
    ok, why = rl.allow("c")
    assert not ok and "capacity" in why


def test_rate_limited_chat_returns_429_json() -> None:
    with TestClient(_app(password="")) as client:
        sid = None
        for _ in range(12):
            r = client.post("/api/chat", json={"message": "hi", "session_id": sid})
            assert r.status_code == 200
            sid = sid or r.headers["x-session-id"]
        r = client.post("/api/chat", json={"message": "hi", "session_id": sid})
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited" and r.headers["retry-after"]
