"""Database readiness probe (AC-2.1.3): 503 while the database is unreachable, 200 when it is
back, without restarting the app."""

import socket

import anyio
import anyio.abc
import httpx
import psycopg
import pytest

from htco_api.app import AppOptions, create_app
from htco_persistence.readiness import DatabaseReadinessCheck
from tests.persistence.database import database_url, parse_host_port


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_database_check__repr__hides_the_connection_string() -> None:
    check = DatabaseReadinessCheck("postgresql://htco:hunter2@db.internal:5432/postgres")

    assert "hunter2" not in repr(check)
    assert check.name == "database"


@pytest.mark.anyio
@pytest.mark.ac("AC-2.1.3")
async def test_database_check__nothing_listening__raises() -> None:
    check = DatabaseReadinessCheck(f"postgresql://htco@127.0.0.1:{_unused_port()}/postgres")

    with pytest.raises(psycopg.OperationalError):
        await check()


class _Relay:
    """A TCP relay in front of Postgres that the test can switch off and on, standing in for a
    database that goes away and comes back while the API keeps running."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self.port = _unused_port()
        self._listener: anyio.abc.SocketListener | None = None
        self._scope: anyio.CancelScope | None = None

    async def _pipe(self, source: anyio.abc.SocketStream, sink: anyio.abc.SocketStream) -> None:
        try:
            async for chunk in source:
                await sink.send(chunk)
        except anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream:
            pass
        finally:
            await sink.aclose()

    async def _handle(self, client: anyio.abc.SocketStream) -> None:
        async with (
            client,
            await anyio.connect_tcp(*self._target) as upstream,
            anyio.create_task_group() as group,
        ):
            group.start_soon(self._pipe, client, upstream)
            group.start_soon(self._pipe, upstream, client)

    async def serve(self, *, task_status: anyio.abc.TaskStatus[None]) -> None:
        with anyio.CancelScope() as scope:
            self._scope = scope
            listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=self.port)
            async with listener:
                task_status.started()
                await listener.serve(self._handle)

    def stop(self) -> None:
        assert self._scope is not None
        self._scope.cancel()


@pytest.mark.anyio
@pytest.mark.database
@pytest.mark.ac("AC-2.1.3")
async def test_readyz__database_goes_away_and_comes_back__503_then_200() -> None:
    host, port = parse_host_port(database_url())
    relay = _Relay(host, port)
    conninfo = database_url().replace(f"{host}:{port}", f"127.0.0.1:{relay.port}")
    app = create_app(
        AppOptions(
            readiness_checks=[DatabaseReadinessCheck(conninfo)], readiness_timeout_seconds=2.0
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        down = await client.get("/readyz")
        async with anyio.create_task_group() as group:
            await group.start(relay.serve)
            up = await client.get("/readyz")
            relay.stop()

    assert down.status_code == 503
    assert down.json()["checks"]["database"] == "failed"
    assert up.status_code == 200
    assert up.json()["checks"]["database"] == "ok"
