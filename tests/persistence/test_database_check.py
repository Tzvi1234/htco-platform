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
