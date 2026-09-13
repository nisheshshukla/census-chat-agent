"""HTTP routes: chat (SSE), session read/reset."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from census_agent.agent.loop import run_turn
from census_agent.app.security import password_ok, rate_limited_response, set_session_cookie
from census_agent.guardrails.judge import judge_answer
from census_agent.logging_setup import request_id_var, session_id_var

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    password: str = Field(max_length=200)


@router.post("/login")
async def login(req: LoginRequest, request: Request) -> Response:
    settings = request.app.state.settings
    ip = request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")
    allowed, _ = request.app.state.rate_limiter.allow(f"login:{ip}")
    if not allowed:
        return JSONResponse(
            status_code=429, content={"error": "rate_limited", "message": "Too many attempts; wait a minute."}
        )
    if not settings.demo_password or not password_ok(settings, req.password.strip()):
        return JSONResponse(
            status_code=401, content={"error": "invalid_password", "message": "That password is not right."}
        )
    resp = JSONResponse(status_code=200, content={"ok": True})
    set_session_cookie(
        resp, settings, secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    )
    return resp


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """Reachable only when authenticated (middleware); the SPA uses it to decide whether to show sign-in."""
    return {"ok": True, "auth_required": bool(request.app.state.settings.demo_password)}


class ChatRequest(BaseModel):
    message: str = Field(max_length=20000)
    session_id: str | None = Field(default=None, max_length=64)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> Response:
    app = request.app
    deps = app.state.agent_deps
    if deps is None:
        raise HTTPException(503, "agent not configured (missing Snowflake or Anthropic credentials)")
    store = app.state.sessions
    session = await store.acquire(req.session_id)
    session_id_var.set(session.id)
    request_id = request_id_var.get()
    allowed, why = app.state.rate_limiter.allow(session.id)
    if not allowed:
        log.warning("rate limited session %s", session.id)
        return rate_limited_response(session.id, request_id, why)
    sem: asyncio.Semaphore = app.state.turn_semaphore

    async def gen() -> AsyncIterator[str]:
        if sem.locked():
            yield _sse({"type": "session", "session_id": session.id, "request_id": request_id})
            yield _sse(
                {
                    "type": "final",
                    "text": "I'm handling several questions at once right now. Please try again in a few seconds.",
                    "error_kind": "busy",
                    "queries": [],
                    "grounding_ok": True,
                }
            )
            return
        async with sem:
            turn = run_turn(deps, session, req.message, request_id)
            try:
                async for event in turn:
                    if await request.is_disconnected():
                        log.info("client disconnected; cancelling turn")
                        break
                    yield _sse(event)
            finally:
                await turn.aclose()
                store.persist(session)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session.id,
        },
    )


@router.get("/session/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    s = await request.app.state.sessions.find(session_id)
    if s is None:
        return {"session_id": session_id, "turns": [], "card": {}}
    return {
        "session_id": s.id,
        "turns": [
            {
                "question": t.question,
                "answer": t.answer,
                "queries": t.queries,
                "trace": t.trace,
                "error_kind": t.error_kind,
                "suggestions": t.suggestions,
                "judge": t.judge,
                "turn_index": i,
            }
            for i, t in enumerate(s.turns)
        ],
        "card": s.card.to_dict(),
    }


@router.post("/session/{session_id}/reset")
async def reset_session(session_id: str, request: Request) -> dict[str, Any]:
    s = await request.app.state.sessions.discard(session_id)
    return {"session_id": s.id, "turns": []}


@router.post("/session/{session_id}/turns/{turn_index}/judge")
async def judge_turn(session_id: str, turn_index: int, request: Request, force: bool = False) -> Any:
    app = request.app
    deps = app.state.agent_deps
    if deps is None:
        raise HTTPException(503, "agent not configured")
    session = await app.state.sessions.find(session_id)
    if session is None or not 0 <= turn_index < len(session.turns):
        raise HTTPException(404, "turn not found")
    turn = session.turns[turn_index]
    if not turn.queries or turn.error_kind:
        raise HTTPException(400, "this answer ran no query; there is nothing to audit")
    allowed, why = app.state.rate_limiter.allow(f"judge:{session.id}")
    if not allowed:
        return rate_limited_response(session.id, request_id_var.get(), why)
    if force or turn.judge is None or turn.judge.get("verdict") == "error":
        turn.judge = await judge_answer(deps.llm, deps.settings.agent_model, turn.question, turn.answer, turn.queries)
        app.state.sessions.persist(session)
    return {"session_id": session.id, "turn_index": turn_index, "judge": turn.judge}
