"""The quote mailbox intake (slice 7.14) and the #192 guards before the intake is turned on.

- Resend inbound's signed `email.received` notice is stored once in the inbox
  (`source = "autova_quotes"`, `event_id` = the mail's Message-ID) with one parse job, so the same
  mail delivered or forwarded twice is one row and one job (AC-7.14.1); an unsigned or wrongly
  signed notice is refused (AC-7.14.3); a notice for another address is kept with no job.
- The job fetches the raw mail and hands it to 7.15's intake; mail from any other sender records
  nothing (AC-7.14.2).
- #192.2: forgery tasks roll up into one per shop per day, with a count.
- #192.3: a genuine AutoVA mail replayed to our mailbox records nothing unless its DKIM-signed
  `To` is the owner's mailbox; otherwise one Human task `unknown_listing`.

All values are invented."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from htco_application.etsy_webhooks import Rejection, WebhookRejectedError, WindowLimiter
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.kernel import TenantShop
from htco_application.quote_mailbox import (
    QUOTE_MAIL_JOB,
    QUOTE_SOURCE,
    MemoryQuoteReviews,
    QuoteMailboxIngress,
    QuoteMailProcessor,
    QuoteMailRateLimitedError,
    RawMailTooLargeError,
    parse_notice,
)
from htco_application.quote_parser import QuoteMail, QuoteParser, signed_recipients
from htco_application.suppliers import IntakeOutcome, QuoteIntake, ShopForMail
from tests.application.test_supplier_quote_intake import ORG, SHOP, Store
from tests.webhook_fakes import SECRET, MemoryWebhookInbox

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parent / "quote_mails"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
MAILBOX = "quotes+k7Rr2xQ9mVb4LpZt@office.example.com"
OWNER = "owner@shop.example.com"
SENDER = "quotes@autova.example.com"
AUTHSERV_ID = "mx.quotes.example.com"
EMAIL_ID = "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def mail_of(text: str) -> QuoteMail:
    return QuoteMail.from_text(text, authserv_id=AUTHSERV_ID, sender=SENDER)


# --- #192.3: the DKIM-signed recipients -----------------------------------------------------------

SIGNED = (
    "DKIM-Signature: v=1; a=rsa-sha256; d=autova.example.com; s=sel1;\n"
    " h=From:To:Subject:Message-ID; bh=ZmFrZQ==; b=ZmFrZQ==\n"
)


def headers(*lines: str) -> str:
    return "From: AutoVA Quotes <quotes@autova.example.com>\n" + "".join(lines) + "\nBody\n"


def test_signed_recipients__one_to_covered_by_the_senders_signature() -> None:
    text = headers("To: Shop Owner <Owner@Shop.Example.com>\n", SIGNED)
    assert signed_recipients(text, sender=SENDER) == ("owner@shop.example.com",)


@pytest.mark.parametrize(
    ("lines", "why"),
    [
        (("To: owner@shop.example.com\n",), "no signature"),
        (("To: owner@shop.example.com\n", SIGNED.replace("From:To:", "From:")), "to not signed"),
        (
            ("To: owner@shop.example.com\n", SIGNED.replace("d=autova", "d=other")),
            "another domain's signature",
        ),
        (
            ("To: owner@shop.example.com\n", "To: someone@else.example.com\n", SIGNED),
            "a second To (the signature covers only one)",
        ),
        (("To: owner@shop.example.com\n", SIGNED, SIGNED), "two signatures for the domain"),
        ((SIGNED,), "no To at all"),
    ],
)
def test_signed_recipients__anything_ambiguous_is_none(lines: tuple[str, ...], why: str) -> None:
    assert signed_recipients(headers(*lines), sender=SENDER) == (), why


def test_from_text__a_multipart_mail__reads_its_plain_text_part() -> None:
    plain = fixture("01_en_usd.eml.txt")
    head, _, body = plain.partition("\n\n")
    multipart = (
        head + "\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=b1\n\n"
        "--b1\nContent-Type: text/plain; charset=utf-8\n\n" + body + "\n"
        "--b1\nContent-Type: text/html; charset=utf-8\n\n<p>Unit price: 1.00 USD</p>\n--b1--\n"
    )
    quote = QuoteParser(SENDER).parse(mail_of(multipart))
    assert getattr(quote, "autova_quote_id", None) == "Q-88213"


# --- The intake with the replay guard and the forgery roll-up -------------------------------------


@dataclass
class World:
    store: Store = field(default_factory=Store)
    reviews: MemoryQuoteReviews = field(default_factory=MemoryQuoteReviews)
    now: datetime = NOW

    @property
    def intake(self) -> QuoteIntake:
        return QuoteIntake(
            parser=QuoteParser(SENDER),
            store=self.store,
            reviews=self.reviews,
            clock=lambda: self.now,
            owner_mailbox=OWNER,
        )


TARGET = ShopForMail(ORG, SHOP)


async def test_replay__signed_to_the_owner__recorded() -> None:
    world = World()
    outcome = await world.intake.handle(mail_of(fixture("01_en_usd.eml.txt")), TARGET)
    assert outcome is IntakeOutcome.NEW_PRODUCT_QUOTE
    assert world.reviews.tasks == {}


async def test_replay__a_genuine_mail_to_someone_else__records_nothing_one_task() -> None:
    world = World()
    text = fixture("01_en_usd.eml.txt").replace(
        "To: Shop Owner <owner@shop.example.com>", "To: another.customer@example.com"
    )
    outcome = await world.intake.handle(mail_of(text), TARGET)
    assert outcome is IntakeOutcome.NEEDS_REVIEW
    assert world.store.quotes == {}
    [task] = world.reviews.tasks.values()
    assert (task.reason, task.message_id, task.count) == (
        "unknown_listing",
        "<q-88213@autova.example.com>",
        1,
    )


async def test_replay__a_to_the_signature_does_not_cover__records_nothing() -> None:
    world = World()
    text = fixture("01_en_usd.eml.txt").replace("h=from:to:", "h=from:")
    outcome = await world.intake.handle(mail_of(text), TARGET)
    assert outcome is IntakeOutcome.NEEDS_REVIEW
    assert world.store.quotes == {}


async def test_forgeries__roll_up_into_one_task_per_shop_per_day_with_a_count() -> None:
    world = World()
    forged = fixture("13_forged_from_unsigned.eml.txt")
    for n in range(5):
        text = forged.replace("<forged-1@", f"<forged-{n}@")
        assert await world.intake.handle(mail_of(text), TARGET) is IntakeOutcome.QUARANTINED
    [task] = world.reviews.tasks.values()
    assert (task.reason, task.count, task.day) == ("unauthenticated_sender", 5, date(2026, 10, 27))

    world.now = NOW + timedelta(days=1)
    await world.intake.handle(mail_of(forged), TARGET)
    other_shop = ShopForMail(ORG, uuid.UUID("71400000-0000-0000-0000-000000000003"))
    await world.intake.handle(mail_of(forged), other_shop)
    assert sorted(t.count for t in world.reviews.tasks.values()) == [1, 1, 5]


async def test_check_this_quote__idempotent_per_mail() -> None:
    reviews = MemoryQuoteReviews()
    for _ in range(2):
        await reviews.check_this_quote(
            ORG, SHOP, message_id="<m@x.example.com>", reason="unknown_field"
        )
    assert len(reviews.tasks) == 1


# --- The ingress (the API process) -------------------------------------------------------------


def notice(**data: Any) -> bytes:
    body: dict[str, Any] = {
        "type": "email.received",
        "created_at": "2026-10-27T12:00:00.000Z",
        "data": {
            "email_id": EMAIL_ID,
            "from": SENDER,
            "to": [OWNER],
            "received_for": [MAILBOX],
            "message_id": "<q-88213@autova.example.com>",
            "subject": "New supplier quote",
            "attachments": [],
        },
    }
    body["data"].update(data)
    return json.dumps(body).encode()


def svix(body: bytes, *, svix_id: str = "msg_resend0001", at: datetime = NOW) -> dict[str, str]:
    timestamp = str(int(at.timestamp()))
    return {
        "svix-id": svix_id,
        "svix-timestamp": timestamp,
        "svix-signature": "v1," + SECRET.sign(svix_id, timestamp, body),
    }


@dataclass
class Shops:
    shop: TenantShop | None = field(default_factory=lambda: TenantShop(ORG, SHOP))

    async def quote_shop(self) -> TenantShop | None:
        return self.shop


def ingress(inbox: MemoryWebhookInbox, shops: Shops | None = None) -> QuoteMailboxIngress:
    return QuoteMailboxIngress(
        secret=SECRET, inbox=inbox, shops=shops or Shops(), mailbox=MAILBOX, clock=lambda: NOW
    )


@pytest.mark.ac("AC-7.14.1")
async def test_ingress__the_same_mail_twice__one_inbox_row_and_one_job() -> None:
    inbox = MemoryWebhookInbox()
    first = await ingress(inbox).receive(svix(notice()), notice())
    # Forwarded again: another Resend email and another notice, the same Message-ID.
    again = notice(email_id="5ef9a417-02e9-4d39-ad75-9611e0fcc33d")
    second = await ingress(inbox).receive(svix(again, svix_id="msg_resend0002"), again)

    assert first is not None
    assert second is not None
    assert (first.duplicate, second.duplicate) == (False, True)
    assert list(inbox.rows) == [(QUOTE_SOURCE, "<q-88213@autova.example.com>")]
    [job] = inbox.queue.jobs.values()
    assert job.spec.kind == QUOTE_MAIL_JOB
    assert job.spec.payload == {"email_id": EMAIL_ID, "message_id": "<q-88213@autova.example.com>"}
    assert (job.spec.organization_id, job.spec.shop_id) == (ORG, SHOP)
    # No sender, subject or recipient is stored: the mail itself is read only by the job.
    [(_, event, _)] = inbox.rows.values()
    assert set(event.payload) == {"email_id", "message_id"}


@pytest.mark.ac("AC-7.14.3")
@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (
            lambda h: {k: v for k, v in h.items() if k != "svix-signature"},
            Rejection.MISSING_HEADERS,
        ),
        (lambda h: {**h, "svix-signature": "v1,AAAA"}, Rejection.BAD_SIGNATURE),
        (
            lambda h: {**h, "svix-timestamp": str(int((NOW - timedelta(hours=1)).timestamp()))},
            Rejection.STALE_TIMESTAMP,
        ),
    ],
)
async def test_ingress__unsigned_or_wrongly_signed__refused(tamper: Any, reason: Rejection) -> None:
    inbox = MemoryWebhookInbox()
    with pytest.raises(WebhookRejectedError) as refused:
        await ingress(inbox).receive(tamper(svix(notice())), notice())
    assert refused.value.reason is reason
    assert inbox.rows == {}


async def test_ingress__another_address__counted_and_never_stored() -> None:
    """Anyone can mail the domain with a Message-ID of their choosing: no row, so no growth."""
    inbox = MemoryWebhookInbox()
    body = notice(received_for=["quotes@office.example.com"], to=["quotes@office.example.com"])
    gate = ingress(inbox)
    assert await gate.receive(svix(body), body) is None
    again = notice(
        received_for=["quotes@office.example.com"], message_id="<chosen-2@elsewhere.example.com>"
    )
    assert await gate.receive(svix(again, svix_id="msg_resend0002"), again) is None
    assert inbox.rows == {}
    assert inbox.queue.jobs == {}
    assert gate.unrouted == {"not_our_mailbox": 2}


async def test_ingress__over_the_rate_limit__refused_then_accepted_after_the_window() -> None:
    inbox = MemoryWebhookInbox()
    clock = [NOW]
    gate = QuoteMailboxIngress(
        secret=SECRET, inbox=inbox, shops=Shops(), mailbox=MAILBOX, clock=lambda: clock[0],
        limiter=WindowLimiter(1),
    )  # fmt: skip
    await gate.receive(svix(notice()), notice())
    second = notice(message_id="<q-88214@autova.example.com>")
    with pytest.raises(QuoteMailRateLimitedError) as limited:
        await gate.receive(svix(second, svix_id="msg_resend0002"), second)
    assert 0 < limited.value.wait <= 1
    assert len(inbox.rows) == 1  # nothing stored for the refused one

    clock[0] = NOW + timedelta(seconds=1)
    accepted = await gate.receive(svix(second, svix_id="msg_resend0002", at=clock[0]), second)
    assert accepted is not None
    assert len(inbox.rows) == 2


async def test_ingress__the_rate_limit_counts_only_signed_notices() -> None:
    gate = QuoteMailboxIngress(
        secret=SECRET, inbox=MemoryWebhookInbox(), shops=Shops(), mailbox=MAILBOX,
        clock=lambda: NOW, limiter=WindowLimiter(1),
    )  # fmt: skip
    for _ in range(3):
        with pytest.raises(WebhookRejectedError):
            await gate.receive({**svix(notice()), "svix-signature": "v1,AAAA"}, notice())
    assert await gate.receive(svix(notice()), notice()) is not None


async def test_ingress__no_connected_quote_shop__kept_with_no_job() -> None:
    inbox = MemoryWebhookInbox()
    gate = ingress(inbox, Shops(None))
    accepted = await gate.receive(svix(notice()), notice())
    assert accepted is not None
    assert accepted.job_id is None
    assert len(inbox.rows) == 1  # kept: it is ours, and runs once the shop is connected
    assert gate.unrouted == {"unknown_shop": 1}


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"type": "email.sent", "data": {}}).encode(),
        notice(email_id="../../admin"),
        notice(message_id="no angle brackets"),
        notice(message_id=None),
        notice(to="not a list"),
    ],
)
def test_parse_notice__not_a_received_mail__bad_payload(body: bytes) -> None:
    with pytest.raises(WebhookRejectedError) as refused:
        parse_notice(body)
    assert refused.value.reason is Rejection.BAD_PAYLOAD


# --- The parse job (the worker) ----------------------------------------------------------------


@dataclass
class Mails:
    raw: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def raw_mail(self, email_id: str) -> str:
        self.asked.append(email_id)
        return self.raw[email_id]


def claimed(payload: dict[str, Any]) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid7(), kind=QUOTE_MAIL_JOB, idempotency_key="k", payload=payload,
        organization_id=ORG, shop_id=SHOP, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW + timedelta(minutes=5),
    )  # fmt: skip


async def test_processor__fetches_the_mail_and_runs_the_intake() -> None:
    world = World()
    mails = Mails({EMAIL_ID: fixture("01_en_usd.eml.txt")})
    processor = QuoteMailProcessor(
        mails=mails, intake=world.intake, authserv_id=AUTHSERV_ID, sender=SENDER
    )
    job = claimed({"email_id": EMAIL_ID, "message_id": "<q-88213@autova.example.com>"})
    assert await processor.process(job) is IntakeOutcome.NEW_PRODUCT_QUOTE
    assert mails.asked == [EMAIL_ID]
    assert len(world.store.quotes) == 1


@pytest.mark.ac("AC-7.14.2")
async def test_processor__another_sender__no_quote() -> None:
    world = World()
    mails = Mails({EMAIL_ID: fixture("09_other_sender.eml.txt")})
    processor = QuoteMailProcessor(
        mails=mails, intake=world.intake, authserv_id=AUTHSERV_ID, sender=SENDER
    )
    outcome = await processor.process(claimed({"email_id": EMAIL_ID, "message_id": "<x@y.z>"}))
    assert outcome is IntakeOutcome.QUARANTINED
    assert world.store.quotes == {}
    assert world.reviews.tasks == {}


@dataclass
class HugeMails:
    asked: int = 0

    async def raw_mail(self, email_id: str) -> str:  # noqa: ARG002 - the port
        self.asked += 1
        raise RawMailTooLargeError("resend raw mail over 1 MiB")


async def test_processor__a_mail_over_the_cap__one_human_task_and_no_retry() -> None:
    """AutoVA mails with inline images can pass 1 MiB: never fits, so no JobError (no retry until
    it dead-letters) but one "check this quote" task naming the mail."""
    world = World()
    mails = HugeMails()
    processor = QuoteMailProcessor(
        mails=mails, intake=world.intake, authserv_id=AUTHSERV_ID, sender=SENDER
    )
    job = claimed({"email_id": EMAIL_ID, "message_id": "<q-88213@autova.example.com>"})

    outcome = await processor.process(job)

    assert outcome is IntakeOutcome.NEEDS_REVIEW
    assert mails.asked == 1
    assert world.store.quotes == {}
    [task] = world.reviews.tasks.values()
    assert (task.reason, task.message_id) == ("too_large", "<q-88213@autova.example.com>")
