"""The worker's `autova.quote_mail` job (slice 7.14) and the owner's mailbox setting (#192)."""

import os
import uuid

import pytest

from htco_application.quote_mailbox import QUOTE_MAIL_JOB, QuoteMailProcessor
from htco_platform.settings import ConfigurationError, load_settings
from htco_worker.main import quote_mail_handlers
from htco_worker.settings import WorkerSettings
from tests.application.test_quote_mailbox import (
    AUTHSERV_ID,
    EMAIL_ID,
    SENDER,
    Mails,
    World,
    claimed,
    fixture,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_ENVIRONMENT", "test")
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


async def test_the_job__runs_the_intake() -> None:
    world = World()
    processor = QuoteMailProcessor(
        mails=Mails({EMAIL_ID: fixture("01_en_usd.eml.txt")}),
        intake=world.intake, authserv_id=AUTHSERV_ID, sender=SENDER,
    )  # fmt: skip
    handler = quote_mail_handlers(processor)[QUOTE_MAIL_JOB]
    await handler(claimed({"email_id": EMAIL_ID, "message_id": "<q-88213@autova.example.com>"}))
    assert len(world.store.quotes) == 1


async def test_the_job__without_its_email_id__fails_without_reading_anything() -> None:
    world, mails = World(), Mails()
    processor = QuoteMailProcessor(
        mails=mails, intake=world.intake, authserv_id=AUTHSERV_ID, sender=SENDER
    )
    with pytest.raises(Exception, match="email ID"):
        await processor.process(claimed({"email_id": str(uuid.uuid4()).upper()}))
    assert mails.asked == []


@pytest.mark.usefixtures("environment")
def test_settings__no_owner_mailbox_by_default() -> None:
    assert load_settings(WorkerSettings).autova_quote_owner_mailbox is None


def test_settings__the_owner_mailbox_is_an_address(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("HTCO_AUTOVA_QUOTE_OWNER_MAILBOX", "owner@shop.example.com")
    assert load_settings(WorkerSettings).autova_quote_owner_mailbox == "owner@shop.example.com"
    environment.setenv("HTCO_AUTOVA_QUOTE_OWNER_MAILBOX", "owner")
    with pytest.raises(ConfigurationError):
        load_settings(WorkerSettings)
