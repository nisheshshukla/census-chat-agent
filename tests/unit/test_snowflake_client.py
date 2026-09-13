from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from census_agent.config import Settings
from census_agent.data import snowflake_client as sc
from census_agent.errors import DataUnavailableError


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.description = [("A",), ("B",)]
        self.sfqid = "q1"
        self._rows = [(1, "x"), (2, "y"), (3, "z")]

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append(sql)
        if "BOOM" in sql:
            import snowflake.connector.errors as e

            raise e.ProgrammingError("SQL compilation error")
        if "NETWORK" in sql:
            raise OSError("socket closed")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return self._rows[:n]

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pem(tmp_path: Any) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    data = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    p = tmp_path / "k.p8"
    p.write_bytes(data)
    return str(p)


def test_private_key_loads_from_path_and_from_env(pem: str) -> None:
    s = Settings(_env_file=None, snowflake_private_key_path=pem)
    der = sc._load_private_key(s)
    assert len(der) > 1000
    s2 = Settings(_env_file=None, snowflake_private_key=Path(pem).read_text())
    assert sc._load_private_key(s2) == der


async def test_pool_reuse_truncation_and_error_paths(monkeypatch: Any, pem: str) -> None:
    conns: list[FakeConn] = []

    def connect(**kwargs: Any) -> FakeConn:
        assert kwargs["session_parameters"]["QUERY_TAG"] == "census-agent"
        c = FakeConn()
        conns.append(c)
        return c

    import snowflake.connector

    monkeypatch.setattr(snowflake.connector, "connect", connect)
    client = sc.SnowflakeClient(Settings(_env_file=None, snowflake_account="X-Y", snowflake_private_key_path=pem))
    client.warm()
    r = await client.query("SELECT 1")
    assert r.columns == ["A", "B"] and len(r.rows) == 3 and r.query_id == "q1" and not r.truncated
    r2 = await client.query("SELECT 1", max_rows=2)
    assert len(r2.rows) == 2 and r2.truncated
    assert len(conns) == 1, "pooled connection was reused"

    import snowflake.connector.errors as e

    with pytest.raises(e.ProgrammingError):
        await client.query("BOOM")
    assert not conns[0].closed, "a SQL error keeps the connection"

    with pytest.raises(DataUnavailableError):
        await client.query("NETWORK")
    assert conns[0].closed, "a transport error discards the connection"
    assert await client.ping() is True and len(conns) == 2


async def test_connect_failure_is_data_unavailable(monkeypatch: Any, pem: str) -> None:
    import snowflake.connector

    def connect(**kwargs: Any) -> Any:
        raise ConnectionError("refused")

    monkeypatch.setattr(snowflake.connector, "connect", connect)
    client = sc.SnowflakeClient(Settings(_env_file=None, snowflake_account="X-Y", snowflake_private_key_path=pem))
    client.warm()
    assert await client.ping() is False
    with pytest.raises(DataUnavailableError):
        await client.query("SELECT 1")
