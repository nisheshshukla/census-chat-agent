from __future__ import annotations

from fastapi.testclient import TestClient

from census_agent.app.main import create_app
from census_agent.config import Settings
from census_agent.data.snowflake_client import QueryResult


class FakeSnowflake:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    async def query(self, sql, params=None, *, max_rows=None):  # noqa: ANN001
        return QueryResult(columns=["1"], rows=[(1,)], elapsed_ms=1)

    async def ping(self) -> bool:
        return self.ok


def _settings(**overrides) -> Settings:  # noqa: ANN003
    base = {"anthropic_api_key": "test-key", "snowflake_account": "X-Y", "demo_password": ""}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_health_ok_when_all_checks_pass() -> None:
    app = create_app(_settings(), snowflake=FakeSnowflake(ok=True))
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["snowflake"]["ok"] is True
    assert body["checks"]["llm"]["ok"] is True
    assert "git_sha" in body
    assert r.headers["x-request-id"]


def test_health_degraded_when_snowflake_down() -> None:
    app = create_app(_settings(), snowflake=FakeSnowflake(ok=False))
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["snowflake"]["message"] == "unreachable"


def test_health_degraded_when_llm_key_missing() -> None:
    app = create_app(_settings(anthropic_api_key=""), snowflake=FakeSnowflake(ok=True))
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["checks"]["llm"]["ok"] is False
