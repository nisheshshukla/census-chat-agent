"""Demo auth and abuse limits.

- HTTP basic auth for everything except /api/health when DEMO_PASSWORD is set. Reviewers get the
  credentials in the README; the health endpoint stays open for the platform probe.
- Per-session sliding-window rate limit on /api/chat, plus a global one, so a stuck client or a
  script cannot drain the LLM budget. Limits are generous for humans.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from census_agent.config import Settings

OPEN_PATHS = ("/api/health", "/api/login")
COOKIE = "census_session"
SESSION_TTL_S = 7 * 24 * 3600


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(("census-agent-session:" + settings.demo_password).encode()).digest()


def make_session_token(settings: Settings, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + SESSION_TTL_S)
    sig = hmac.new(_key(settings), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_session_token(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _, sig = token.partition(".")
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    expected = hmac.new(_key(settings), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def password_ok(settings: Settings, password: str) -> bool:
    return hmac.compare_digest(password.encode(), settings.demo_password.encode())


def _basic_ok(request: Request, settings: Settings) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(user.encode(), settings.demo_user.encode()) and password_ok(settings, password)


def check_auth(request: Request, settings: Settings) -> Response | None:
    """API routes require a signed cookie (in-app sign-in) or basic auth (scripts).
    Static assets and the page itself are open so the sign-in screen can load."""
    path = request.url.path
    if not settings.demo_password or not path.startswith("/api/") or path.startswith(OPEN_PATHS):
        return None
    if verify_session_token(settings, request.cookies.get(COOKIE)) or _basic_ok(request, settings):
        return None
    return JSONResponse(status_code=401, content={"error": "unauthorized", "message": "Sign in required"})


def set_session_cookie(response: Response, settings: Settings, secure: bool) -> None:
    response.set_cookie(
        COOKIE,
        make_session_token(settings),
        max_age=SESSION_TTL_S,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


class RateLimiter:
    def __init__(self, per_session: int = 12, global_limit: int = 60, window_s: float = 60.0) -> None:
        self.per_session = per_session
        self.global_limit = global_limit
        self.window_s = window_s
        self._sessions: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window_s:
            q.popleft()

    def allow(self, key: str) -> tuple[bool, str]:
        now = time.monotonic()
        self._prune(self._global, now)
        if len(self._global) >= self.global_limit:
            return False, "The assistant is at capacity right now. Please try again in a minute."
        q = self._sessions.setdefault(key, deque())
        self._prune(q, now)
        if len(q) >= self.per_session:
            return False, "You've sent a lot of questions in the last minute. Please wait a moment and try again."
        q.append(now)
        self._global.append(now)
        if len(self._sessions) > 5000:
            for k in [k for k, v in self._sessions.items() if not v][:1000]:
                self._sessions.pop(k, None)
        return True, ""


def rate_limited_response(session_id: str, request_id: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "message": message, "session_id": session_id, "request_id": request_id},
        headers={"Retry-After": "30"},
    )
