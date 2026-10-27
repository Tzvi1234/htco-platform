"""`POST /webhooks/autova-quotes` (slice 7.14): off by default, Resend's Svix signature checked on
the raw body (AC-7.14.3), the notice stored once with its job, and the settings all or nothing.
In-memory inbox; all values are invented."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from htco_api.app import AppOptions, create_app
from htco_api.settings import ApiSettings
from htco_application.quote_mailbox import QuoteMailboxIngress
from tests.application.test_quote_mailbox import MAILBOX, NOW, Shops, notice, svix
from tests.webhook_fakes import SECRET, SECRET_TEXT, MemoryWebhookInbox

pytestmark = pytest.mark.anyio

PATH = "/webhooks/autova-quotes"
PROBLEMS = "https://ht-co.app/problems/"


def app(inbox: MemoryWebhookInbox, *, enabled: bool = True) -> FastAPI:
    ingress = QuoteMailboxIngress(
        secret=SECRET, inbox=inbox, shops=Shops(), mailbox=MAILBOX, clock=lambda: NOW
    )
    return create_app(AppOptions(autova_quotes=ingress if enabled else None, clock=lambda: NOW))


def settings(**values: object) -> ApiSettings:
    return ApiSettings(environment="test", **values)  # type: ignore[arg-type]


async def post(application: FastAPI, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        return await client.post(PATH, content=body, headers=headers)


async def test_off_by_default__404() -> None:
    inbox = MemoryWebhookInbox()
    response = await post(app(inbox, enabled=False), notice(), svix(notice()))
    assert response.status_code == 404
    assert inbox.rows == {}


async def test_a_signed_notice__204_stored_with_its_job() -> None:
    inbox = MemoryWebhookInbox()
    response = await post(app(inbox), notice(), svix(notice()))
    assert response.status_code == 204
    assert len(inbox.queue.jobs) == 1


@pytest.mark.ac("AC-7.14.3")
async def test_a_wrongly_signed_notice__401_and_nothing_stored() -> None:
    inbox = MemoryWebhookInbox()
    headers = {**svix(notice()), "svix-signature": "v1,AAAA"}
    response = await post(app(inbox), notice(), headers)
    assert response.status_code == 401
    assert response.json()["type"] == PROBLEMS + "webhook-signature-invalid"
    assert inbox.rows == {}


async def test_signed_but_not_a_received_mail__400() -> None:
    body = b'{"type": "email.sent", "data": {}}'
    response = await post(app(MemoryWebhookInbox()), body, svix(body))
    assert response.status_code == 400


def test_settings__the_quote_mailbox_is_all_or_nothing() -> None:
    assert not settings().autova_quote_intake_enabled
    full = settings(
        autova_quote_webhook_secret=SECRET_TEXT,
        autova_quote_mailbox=MAILBOX,
        autova_quote_etsy_shop_id=10000001,
    )
    assert full.autova_quote_intake_enabled
    with pytest.raises(ValidationError):
        settings(autova_quote_webhook_secret=SECRET_TEXT)


@pytest.mark.parametrize(
    "mailbox",
    ["quotes@office.example.com", "quotes+short@office.example.com", "not an address"],
)
def test_settings__the_mailbox_carries_a_secret_token(mailbox: str) -> None:
    with pytest.raises(ValidationError, match="secret token"):
        settings(
            autova_quote_webhook_secret=SECRET_TEXT,
            autova_quote_mailbox=mailbox,
            autova_quote_etsy_shop_id=10000001,
        )
