"""The database readiness check (slice 2.1, AC-2.1.3)."""

import socket
import time

import psycopg
import pytest

from htco_persistence.readiness import DatabaseCheck


def _closed_port() -> int:
    """A local port with nothing listening (bound, then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.anyio
async def test_database_check__nothing_listening__raises_quickly() -> None:
    check = DatabaseCheck(f"postgresql://user@127.0.0.1:{_closed_port()}/db", connect_timeout=1)

    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        await check()

    assert time.monotonic() - started < 5


def test_database_check__is_named_database() -> None:
    assert DatabaseCheck("postgresql://user@localhost/db").name == "database"


def test_database_check__repr__hides_the_connection_string() -> None:
    check = DatabaseCheck("postgresql://user:hunter2@db.example:5432/postgres")

    assert "hunter2" not in repr(check)
    assert "db.example" not in repr(check)


@pytest.mark.anyio
async def test_database_check__stalled_probe__is_abandoned_by_the_caller_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that accepts the connection and then stalls must not hang `/readyz`."""
    import anyio  # noqa: PLC0415 - local to this test

    check = DatabaseCheck("postgresql://user@127.0.0.1:1/db")
    monkeypatch.setattr(check, "_probe", lambda: time.sleep(3))

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.2):
            await check()
    assert time.monotonic() - started < 1


def test_database_check__bounds_the_statement_as_well_as_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            seen["sql"] = sql

    def connect(_conninfo: str, **options: object) -> _Connection:
        seen.update(options)
        return _Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    DatabaseCheck("postgresql://user@db.invalid/db", connect_timeout=1)._probe()

    assert seen["connect_timeout"] == 1
    assert "statement_timeout=1000" in str(seen["options"])
