"""The email adapters (slice 6.4): Resend against a mock transport only (never the network),
and the default log adapter that sends nothing."""

import json

import httpx
import pytest

from htco_application.notifications import (
    EmailMessage,
    EmailPermanentError,
    EmailTemporaryError,
)
from htco_integrations.email import RESEND_URL, LogEmailAdapter, ResendEmailAdapter
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

KEY = "re_fake_key_for_tests"
MESSAGE = EmailMessage(
    to="owner@example.test",
    subject="Order 7: needs attention",
    text="text",
    html="<p>html</p>",
    idempotency_key="email:incident_opened:x:user",
    reply_to="support@example.test",
    tags={"kind": "incident_opened"},
)


def adapter(handler: httpx.MockTransport) -> ResendEmailAdapter:
    return ResendEmailAdapter(httpx.AsyncClient(transport=handler), api_key=KEY)


@pytest.mark.ac("AC-6.4.1")
async def test_resend_gets_the_outbox_key_as_its_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re-msg-1"})

    receipt = await adapter(httpx.MockTransport(answer)).send(MESSAGE)

    assert receipt.provider_id == "re-msg-1"
    [request] = seen
    assert str(request.url) == RESEND_URL
    assert request.method == "POST"
    assert request.headers["Idempotency-Key"] == MESSAGE.idempotency_key
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["reply_to"] == "support@example.test"
    assert body["from"].endswith("<noreply@office.ht-co.app>")
    assert body["to"] == ["owner@example.test"]


@pytest.mark.ac("AC-6.4.3")
@pytest.mark.parametrize("status", [409, 429, 500, 503])
async def test_resend_busy_or_down_is_temporary(status: int) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={"message": "x"}))
    with pytest.raises(EmailTemporaryError, match=str(status)):
        await adapter(transport).send(MESSAGE)


@pytest.mark.ac("AC-6.4.3")
@pytest.mark.parametrize("status", [400, 403, 422])
async def test_resend_refusing_the_message_is_permanent_without_the_address(status: int) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, json={"message": "bad owner@example.test"})
    )
    with pytest.raises(EmailPermanentError) as raised:
        await adapter(transport).send(MESSAGE)
    assert "owner@example.test" not in str(raised.value)


async def test_network_errors_and_odd_answers_are_temporary() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(EmailTemporaryError, match="ConnectError"):
        await adapter(httpx.MockTransport(fail)).send(MESSAGE)
    with pytest.raises(EmailTemporaryError, match="without an id"):
        await adapter(httpx.MockTransport(lambda _: httpx.Response(200, json={}))).send(MESSAGE)


def test_the_key_is_checked_and_hidden() -> None:
    with pytest.raises(ValueError, match="re_"):
        ResendEmailAdapter(httpx.AsyncClient(), api_key="sk_wrong")
    assert KEY not in repr(ResendEmailAdapter(httpx.AsyncClient(), api_key=KEY))


async def test_the_log_adapter_sends_nothing_and_logs_no_address() -> None:
    log = LogEmailAdapter()
    with capture_logs() as logs:
        receipt = await log.send(MESSAGE)
    assert receipt.provider_id == "log:1"
    assert log.sent == [MESSAGE.idempotency_key]
    assert "owner@example.test" not in logs.raw()
    assert "Order 7" not in logs.raw()
