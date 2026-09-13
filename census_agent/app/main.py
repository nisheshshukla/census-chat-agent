"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from census_agent import __version__
from census_agent.agent.cache import TTLCache
from census_agent.agent.loop import AgentDeps
from census_agent.app.persistence import SessionRepository
from census_agent.app.routes import router
from census_agent.app.security import RateLimiter, check_auth
from census_agent.app.sessions import SessionStore
from census_agent.config import Settings, get_settings
from census_agent.data.catalog import load_catalog
from census_agent.data.field_search import build_field_search
from census_agent.data.geography import load_resolver
from census_agent.data.snowflake_client import SnowflakeClient, SnowflakeClientProtocol
from census_agent.logging_setup import configure_logging, log_event, request_id_var

log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


class HealthCache:
    """Health probes run every few seconds on Fly; cache the Snowflake ping briefly."""

    def __init__(self, ttl_s: float = 30.0) -> None:
        self.ttl_s = ttl_s
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    def get(self) -> dict[str, Any] | None:
        if self._value is not None and time.monotonic() - self._at < self.ttl_s:
            return self._value
        return None

    def set(self, value: dict[str, Any]) -> None:
        self._value = value
        self._at = time.monotonic()


def build_deps(
    settings: Settings,
    snowflake: SnowflakeClientProtocol | None,
    llm: anthropic.AsyncAnthropic | None,
) -> AgentDeps | None:
    if snowflake is None or llm is None:
        return None
    catalog = load_catalog()
    field_search = build_field_search(catalog, snowflake, settings.field_search_backend)
    resolver = load_resolver()
    return AgentDeps.build(
        settings,
        catalog,
        field_search,
        resolver,
        snowflake,
        llm,
        TTLCache(max_items=512, ttl_s=3600),
    )


def create_app(
    settings: Settings | None = None,
    snowflake: SnowflakeClientProtocol | None = None,
    llm: anthropic.AsyncAnthropic | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client: SnowflakeClientProtocol | None = snowflake
        if client is None and settings.snowflake_configured:
            sf = SnowflakeClient(settings)
            await asyncio.to_thread(sf.warm)
            client = sf
        llm_client = llm
        if llm_client is None and settings.anthropic_configured:
            llm_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=1, timeout=45.0)
        app.state.snowflake = client
        app.state.settings = settings
        app.state.health_cache = HealthCache()
        repository = SessionRepository(client) if client is not None else None
        if repository is not None:
            await repository.prepare()
        app.state.session_repository = repository
        app.state.sessions = SessionStore(ttl_s=settings.session_ttl_s, repository=repository)
        app.state.turn_semaphore = asyncio.Semaphore(settings.max_concurrent_turns)
        app.state.rate_limiter = RateLimiter()
        app.state.agent_deps = build_deps(settings, client, llm_client)
        log_event(
            log,
            "app started",
            version=__version__,
            git_sha=settings.git_sha,
            agent_ready=app.state.agent_deps is not None,
            search_backend=settings.field_search_backend,
            session_persistence=bool(repository and repository.enabled),
        )
        yield

    app = FastAPI(title="Census Chat Agent", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            denied = check_auth(request, settings)
            response = denied if denied is not None else await call_next(request)
        except Exception:
            log.exception("unhandled error")
            response = JSONResponse(status_code=500, content={"error": "internal", "request_id": rid})
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif not path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        if not path.startswith("/api/health"):
            log_event(
                log,
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=int((time.perf_counter() - t0) * 1000),
            )
        return response

    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        cache: HealthCache = request.app.state.health_cache
        cached = cache.get()
        if cached is not None:
            return JSONResponse(cached, status_code=200 if cached["status"] == "ok" else 503)

        client: SnowflakeClientProtocol | None = request.app.state.snowflake
        if client is None:
            sf = {"ok": False, "message": "Snowflake not configured"}
        else:
            ok = await client.ping()
            sf = {"ok": ok, "message": "reachable" if ok else "unreachable"}
        llm_check = {
            "ok": settings.anthropic_configured,
            "message": "configured" if settings.anthropic_configured else "missing API key",
        }
        status = "ok" if sf["ok"] and llm_check["ok"] else "degraded"
        body = {
            "status": status,
            "version": __version__,
            "git_sha": settings.git_sha,
            "checks": {"snowflake": sf, "llm": llm_check},
            "search_backend": settings.field_search_backend,
            "sessions": len(request.app.state.sessions),
            "session_persistence": bool(getattr(request.app.state.session_repository, "enabled", False)),
        }
        cache.set(body)
        return JSONResponse(body, status_code=200 if status == "ok" else 503)

    app.include_router(router)

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()
