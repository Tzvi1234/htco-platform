"""`/readyz` against a real Postgres (slice 2.1, AC-2.1.3). Needs `HTCO_TEST_DATABASE_URL`.

Run with `just test-db` after `just db-start`; CI runs it in the `database` job. A TCP relay in
front of the database plays the outage: closing it makes the database unreachable, opening it
brings it back, and the app is never restarted.
"""

import os
import socket
import threading
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_persistence.readiness import DatabaseCheck

pytestmark = pytest.mark.db


class Relay:
    """Forward local TCP connections to the database while `open` is true."""

    def __init__(self, target: tuple[str, int]) -> None:
        self.target = target
        self.open = True
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = int(self._server.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            if not self.open:
                client.close()
                continue
            upstream = socket.create_connection(self.target)
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                sock.close()

    def close(self) -> None:
        self._server.close()


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set: run `just db-start`, then `just test-db`")
    return url


@pytest.fixture
def relay(database_url: str) -> Iterator[Relay]:
    parts = urlsplit(database_url)
    relay = Relay((parts.hostname or "127.0.0.1", parts.port or 5432))
    yield relay
    relay.close()


def _via(database_url: str, relay: Relay) -> str:
    parts = urlsplit(database_url)
    credentials = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{credentials}@127.0.0.1:{relay.port}"))


@pytest.mark.anyio
@pytest.mark.ac("AC-2.1.3")
async def test_readyz__database_down_then_back__503_then_200_without_restart(
    database_url: str, relay: Relay
) -> None:
    check = DatabaseCheck(_via(database_url, relay), connect_timeout=1)
    app = create_app(AppOptions(readiness_checks=[check], readiness_timeout_seconds=2))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/readyz")).status_code == 200

        relay.open = False
        down = await client.get("/readyz")
        assert down.status_code == 503
        assert down.json()["checks"]["database"] == "failed"

        relay.open = True
        assert (await client.get("/readyz")).status_code == 200
