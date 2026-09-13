"""Durable session storage in the app's Snowflake database.

Memory stays the cache; this repository is the source of truth across restarts. Writes happen
after the response is sent and never fail a turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from census_agent.app.sessions import ContextCard, Session, TurnRecord
from census_agent.data.snowflake_client import SnowflakeClientProtocol

log = logging.getLogger(__name__)

RETENTION_DAYS = 30


def session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "created": session.created,
        "last_used": session.last_used,
        "card": asdict(session.card),
        "turns": [asdict(t) for t in session.turns],
    }


def session_from_dict(d: dict[str, Any]) -> Session:
    card = ContextCard(**d.get("card", {}))
    turns = [TurnRecord(**t) for t in d.get("turns", [])]
    return Session(
        id=d["id"],
        created=float(d.get("created", time.time())),
        last_used=float(d.get("last_used", time.time())),
        turns=turns,
        card=card,
    )


class SessionRepository:
    def __init__(self, snowflake: SnowflakeClientProtocol, table: str = "CENSUS_APP_DB.SEMANTIC.SESSIONS") -> None:
        self.snowflake = snowflake
        self.table = table
        self.enabled = False

    async def prepare(self) -> bool:
        try:
            await self.snowflake.query(
                f"CREATE TABLE IF NOT EXISTS {self.table} "
                "(SESSION_ID STRING NOT NULL, UPDATED_AT TIMESTAMP_NTZ NOT NULL, PAYLOAD VARIANT NOT NULL)"
            )
            await self.snowflake.query(
                f"DELETE FROM {self.table} WHERE UPDATED_AT < DATEADD(day, -{RETENTION_DAYS}, CURRENT_TIMESTAMP())"
            )
            self.enabled = True
        except Exception as exc:  # noqa: BLE001
            log.warning("session persistence disabled: %s", str(exc)[:200])
            self.enabled = False
        return self.enabled

    async def save(self, session: Session) -> None:
        if not self.enabled:
            return
        payload = json.dumps(session_to_dict(session), default=str)
        try:
            await self.snowflake.query(
                f"MERGE INTO {self.table} t USING (SELECT %s AS SESSION_ID, PARSE_JSON(%s) AS PAYLOAD) s "
                "ON t.SESSION_ID = s.SESSION_ID "
                "WHEN MATCHED THEN UPDATE SET t.PAYLOAD = s.PAYLOAD, t.UPDATED_AT = CURRENT_TIMESTAMP() "
                "WHEN NOT MATCHED THEN INSERT (SESSION_ID, UPDATED_AT, PAYLOAD) "
                "VALUES (s.SESSION_ID, CURRENT_TIMESTAMP(), s.PAYLOAD)",
                [session.id, payload],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("session save failed for %s: %s", session.id, str(exc)[:200])

    def save_later(self, session: Session) -> None:
        if self.enabled:
            asyncio.create_task(self.save(session))

    async def load(self, session_id: str) -> Session | None:
        if not self.enabled:
            return None
        try:
            res = await self.snowflake.query(
                f"SELECT TO_JSON(PAYLOAD) FROM {self.table} WHERE SESSION_ID = %s", [session_id], max_rows=1
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("session load failed for %s: %s", session_id, str(exc)[:200])
            return None
        if not res.rows:
            return None
        raw = res.rows[0][0]
        data = json.loads(raw) if isinstance(raw, str) else raw
        return session_from_dict(data)

    async def delete(self, session_id: str) -> None:
        if not self.enabled:
            return
        try:
            await self.snowflake.query(f"DELETE FROM {self.table} WHERE SESSION_ID = %s", [session_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("session delete failed for %s: %s", session_id, str(exc)[:200])
