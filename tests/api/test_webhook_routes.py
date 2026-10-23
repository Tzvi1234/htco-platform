"""`POST /webhooks/etsy` (slice 5.3): off by default, verified on the raw body, rate limited,
answered at once, and logged without the body. In-memory inbox; all values are invented."""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import anyio
import httpx
import pytest
from fastapi import FastAPI

from htco_api.app import AppOptions, create_app
from htco_application.etsy_webhooks import MAX_BODY_BYTES, EtsyWebhookIngress, WindowLimiter
from tests.log_capture import capture_logs
from tests.webhook_fakes import (
    NOW,
    SECRET,
    KnownShops,
    MemoryWebhookInbox,
    body_of,
    recorded_events,
    signed,
)

pytestmark = pytest.mark.anyio

PATH = "/webhooks/etsy"
PROBLEMS = "https://ht-co.app/problems/"


@dataclass
class Server:
    inbox: MemoryWebhookInbox = field(default_factory=MemoryWebhookInbox)
    now: datetime = NOW
    limit: int = 1000
    enabled: bool = True

    def app(self) -> FastAPI:
        ingress = EtsyWebhookIngress(
            secret=SECRET, inbox=self.inbox, shops=KnownShops(), clock=lambda: self.now
        )
        return create_app(
            AppOptions(
                etsy_webhooks=ingress if self.enabled else None,
                etsy_webhook_limiter=WindowLimiter(self.limit),
                clock=lambda: self.now,
            )
        )


@pytest.fixture
def server() -> Server:
    return Server()


async def _client(server: Server) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=server.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


@pytest.fixture
async def client(server: Server) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(server):
        yield c


def _event(kind: str = "order.paid") -> bytes:
    return body_of(recorded_events()[kind])


async def test_webhook__off_by_default__answers_404() -> None:
    server = Server(enabled=False)
    async for client in _client(server):
        body = _event()
        response = await client.post(PATH, content=body, headers=signed(body))
        assert response.status_code == 404
        assert response.json()["type"] == PROBLEMS + "not-found"
    assert server.inbox.rows == {}


@pytest.mark.ac("AC-5.3.4")
async def test_webhook__a_signed_event__204_stored_with_its_job(
    client: httpx.AsyncClient, server: Server
) -> None:
    body = _event()

    response = await client.post(PATH, content=body, headers=signed(body))

    assert response.status_code == 204
    assert response.content == b""
    assert len(server.inbox.rows) == 1
    assert len(server.inbox.queue.jobs) == 1


@pytest.mark.ac("AC-5.3.1")
async def test_webhook__replayed_webhook_id__204_and_ignored(
    client: httpx.AsyncClient, server: Server
) -> None:
    body = _event()
    for _ in range(3):
        response = await client.post(PATH, content=body, headers=signed(body))
        assert response.status_code == 204
    assert len(server.inbox.rows) == 1
    assert len(server.inbox.queue.jobs) == 1


@pytest.mark.ac("AC-5.3.1")
@pytest.mark.parametrize("change", ["modified_body", "bad_signature", "stale", "no_headers"])
async def test_webhook__unverified__401_nothing_stored_logged_without_the_body(
    client: httpx.AsyncClient, server: Server, change: str
) -> None:
    body = _event()
    headers = signed(body)
    if change == "modified_body":
        body = _event("order.canceled")
    elif change == "bad_signature":
        headers["webhook-signature"] = "v1,Zm9yZ2Vk"
    elif change == "stale":
        headers = signed(body, at=NOW - timedelta(minutes=6))
    else:
        headers = {"content-type": "application/json"}

    with capture_logs() as logs:
        response = await client.post(PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "webhook-signature-invalid"
    assert server.inbox.rows == {}
    rejected = [line for line in logs.lines() if line["event"] == "etsy.webhook_rejected"]
    assert rejected
    assert "openapi.etsy.com" not in logs.raw()  # the body's resource URL
    assert headers.get("webhook-signature", "v1,none")[3:] not in logs.raw()


async def test_webhook__signed_but_not_an_event__400(
    client: httpx.AsyncClient, server: Server
) -> None:
    body = b'{"hello": "world"}'
    response = await client.post(PATH, content=body, headers=signed(body))
    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "webhook-invalid"
    assert server.inbox.rows == {}


@pytest.mark.ac("AC-5.3.1")
async def test_webhook__over_64_kib__413_before_the_signature_is_checked(
    client: httpx.AsyncClient, server: Server
) -> None:
    body = b"{" + b" " * MAX_BODY_BYTES + b"}"
    response = await client.post(PATH, content=body, headers=signed(body))
    assert response.status_code == 413
    assert response.json()["type"] == PROBLEMS + "webhook-too-large"
    assert server.inbox.rows == {}


async def test_webhook__chunked_body_over_the_limit__413() -> None:
    server = Server()

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b" " * 4096

    async for client in _client(server):
        response = await client.post(PATH, content=chunks(), headers=signed(b""))
        assert response.status_code == 413


@pytest.mark.ac("AC-5.3.3")
async def test_webhook__over_the_rate_limit__429_with_retry_after_then_accepted() -> None:
    server = Server(limit=2)
    async for client in _client(server):
        bodies = [_event(kind) for kind in ("order.paid", "order.shipped", "order.delivered")]
        statuses = []
        with capture_logs() as logs:
            for n, body in enumerate(bodies):
                headers = signed(body, webhook_id=f"msg_fake000{n}")
                statuses.append(await client.post(PATH, content=body, headers=headers))
        assert [r.status_code for r in statuses] == [204, 204, 429]
        assert statuses[2].headers["retry-after"] == "1"
        assert statuses[2].json()["type"] == PROBLEMS + "webhook-rate-limited"
        assert any(line.get("reason") == "rate_limited" for line in logs.lines())
        # Etsy retries after the window: accepted.
        server.now += timedelta(seconds=1)
        retried = await client.post(
            PATH, content=bodies[2], headers=signed(bodies[2], webhook_id="msg_fake0002")
        )
        assert retried.status_code == 204
    assert len(server.inbox.rows) == 3


@pytest.mark.ac("AC-5.3.2")
async def test_webhook__20_concurrent_valid_webhooks__p95_under_one_second() -> None:
    server = Server()
    durations: list[float] = []
    async for client in _client(server):

        async def send(n: int, client: httpx.AsyncClient = client) -> None:
            body = body_of(recorded_events()["listing.updated"] | {"listing_id": 1500000100 + n})
            headers = signed(body, webhook_id=f"msg_load{n:04d}")
            started = time.perf_counter()
            response = await client.post(PATH, content=body, headers=headers)
            durations.append(time.perf_counter() - started)
            assert response.status_code == 204

        async with anyio.create_task_group() as group:
            for n in range(20):
                group.start_soon(send, n)

    durations.sort()
    p95 = durations[int(len(durations) * 0.95) - 1]
    assert p95 < 1.0
    assert len(server.inbox.rows) == 20
