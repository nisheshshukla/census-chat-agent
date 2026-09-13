"""Snowflake access via a key-pair service user.

One small connection pool, synchronous connector calls run in a worker thread so the async
app never blocks. Every query carries a query tag and the per-session statement timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization

from census_agent.config import Settings
from census_agent.errors import DataUnavailableError

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    elapsed_ms: int
    query_id: str | None = None
    truncated: bool = False

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


class SnowflakeClientProtocol(Protocol):
    async def query(
        self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int | None = None
    ) -> QueryResult: ...
    async def ping(self) -> bool: ...


def _load_private_key(settings: Settings) -> bytes:
    if settings.snowflake_private_key:
        pem = settings.snowflake_private_key.encode()
    else:
        with open(settings.snowflake_private_key_path, "rb") as f:
            pem = f.read()
    key = serialization.load_pem_private_key(pem, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@dataclass
class SnowflakeClient:
    settings: Settings
    pool_size: int = 3
    _pool: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sem: threading.Semaphore | None = None

    def __post_init__(self) -> None:
        self._sem = threading.Semaphore(self.pool_size)

    def _connect(self) -> Any:
        import snowflake.connector

        s = self.settings
        try:
            conn = snowflake.connector.connect(
                account=s.snowflake_account,
                user=s.snowflake_user,
                private_key=_load_private_key(s),
                role=s.snowflake_role,
                warehouse=s.snowflake_warehouse,
                database=s.snowflake_app_database or None,
                schema=s.snowflake_app_schema or None,
                client_session_keep_alive=True,
                login_timeout=15,
                network_timeout=30,
                session_parameters={
                    "QUERY_TAG": "census-agent",
                    "STATEMENT_TIMEOUT_IN_SECONDS": s.snowflake_statement_timeout_s,
                },
            )
        except Exception as exc:  # noqa: BLE001 - connector raises many types
            raise DataUnavailableError(f"snowflake connect failed: {type(exc).__name__}: {exc}") from exc
        return conn

    def _acquire(self) -> Any:
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return self._connect()

    def _release(self, conn: Any, *, broken: bool = False) -> None:
        if broken:
            with contextlib.suppress(Exception):
                conn.close()
            return
        with self._lock:
            if len(self._pool) < self.pool_size:
                self._pool.append(conn)
                return
        conn.close()

    def _query_sync(self, sql: str, params: Sequence[Any] | None, max_rows: int | None) -> QueryResult:
        assert self._sem is not None
        with self._sem:
            conn = self._acquire()
            broken = False
            t0 = time.perf_counter()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, params)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    if max_rows is None:
                        rows = cur.fetchall()
                        truncated = False
                    else:
                        rows = cur.fetchmany(max_rows + 1)
                        truncated = len(rows) > max_rows
                        rows = rows[:max_rows]
                    return QueryResult(
                        columns=columns,
                        rows=[tuple(r) for r in rows],
                        elapsed_ms=int((time.perf_counter() - t0) * 1000),
                        query_id=getattr(cur, "sfqid", None),
                        truncated=truncated,
                    )
                finally:
                    cur.close()
            except DataUnavailableError:
                broken = True
                raise
            except Exception as exc:  # noqa: BLE001
                import snowflake.connector.errors as sf_errors

                if isinstance(exc, sf_errors.ProgrammingError):
                    raise
                broken = True
                raise DataUnavailableError(f"snowflake query failed: {type(exc).__name__}: {exc}") from exc
            finally:
                self._release(conn, broken=broken)

    async def query(self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int | None = None) -> QueryResult:
        return await asyncio.to_thread(self._query_sync, sql, params, max_rows)

    async def ping(self) -> bool:
        try:
            await asyncio.wait_for(self.query("SELECT 1"), timeout=8.0)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("snowflake ping failed: %s", exc)
            return False

    def warm(self) -> None:
        """Open one connection eagerly so the first user turn does not pay login latency."""
        try:
            conn = self._connect()
            self._release(conn)
        except DataUnavailableError as exc:
            log.warning("snowflake warm-up failed: %s", exc.detail)
