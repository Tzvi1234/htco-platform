"""The quote mailbox intake (slice 7.14): AutoVA's quote mails in once, then 7.15's parser.

The owner's mailbox forwards AutoVA's quote mails to our quote address (D-23 (a)), which is
Resend inbound. Resend then calls `POST /webhooks/autova-quotes` with a signed `email.received`
notice; `QuoteMailboxIngress.receive` takes it:

1. **Verify** the signature before anything else is read. Resend signs with Svix, which is the
   Standard Webhooks scheme under `svix-*` header names: the same check as Etsy's (5.3), with its
   own secret (`HTCO_AUTOVA_QUOTE_WEBHOOK_SECRET`). Unsigned or wrongly signed: refused
   (AC-7.14.3).
2. **Check the address.** The notice must name our quote address, which carries a secret token
   (`quotes+<token>@`, `HTCO_AUTOVA_QUOTE_MAILBOX`): mail sent straight to the domain by anyone
   else, without the token, is kept with no job and counted.
3. **Record** the notice once in the inbox (`source = "autova_quotes"`, `event_id` = the mail's
   Message-ID), with one parse job keyed by it, in one transaction. The same mail delivered or
   forwarded twice has the same Message-ID: one row, one job (AC-7.14.1). Only Resend's email ID
   and the Message-ID are stored; the mail itself (DC2) is read only by the job and never stored,
   logged or shown to a model.

The worker's `autova.quote_mail` job (`QuoteMailProcessor`) fetches the raw mail from Resend,
reads it (`QuoteMail.from_text`: our server's DKIM verdict and the signed `To`) and hands it to
`QuoteIntake` (7.15). Mail from any sender other than the configured one records nothing
(AC-7.14.2).
"""

import json
import logging
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from htco_application.etsy_webhooks import (
    Accepted,
    Rejection,
    WebhookInbox,
    WebhookRejectedError,
    WebhookSecret,
    WebhookShops,
    verify_signature,
)
from htco_application.jobs import ClaimedJob, JobSpec
from htco_application.kernel import JobError, JsonValue, Offload, TenantShop, run_inline
from htco_application.messaging import InboxEvent
from htco_application.quote_parser import PLAIN_MESSAGE_ID, QuarantineReason, QuoteMail
from htco_application.suppliers import IntakeOutcome, QuoteIntake, ShopForMail

QUOTE_SOURCE = "autova_quotes"
QUOTE_MAIL_JOB = "autova.quote_mail"
RECEIVED = "email.received"
# Resend's email IDs are UUIDs; nothing else is put in the API path.
_EMAIL_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_RECIPIENTS = 50

_log = logging.getLogger(__name__)


def standard_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Svix's `svix-*` headers under the Standard Webhooks names `verify_signature` reads."""
    lower = {name.lower(): value for name, value in headers.items()}
    return {
        f"webhook-{part}": lower.get(f"svix-{part}", "")
        for part in ("id", "timestamp", "signature")
    }


@dataclass(frozen=True)
class QuoteMailNotice:
    """What a verified `email.received` notice says: which mail, and for whom."""

    email_id: str
    message_id: str
    recipients: frozenset[str]  # `to` and `received_for`, lower case; matched, never stored


def _addresses(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_RECIPIENTS:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    return [item.strip().lower() for item in value if isinstance(item, str)]


def parse_notice(body: bytes) -> QuoteMailNotice:
    """The notice from a verified body. Raises `WebhookRejectedError(BAD_PAYLOAD)`."""
    try:
        notice: Any = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD) from None
    if not isinstance(notice, dict) or notice.get("type") != RECEIVED:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    data = notice.get("data")
    if not isinstance(data, dict):
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    email_id, message_id = data.get("email_id"), data.get("message_id")
    if not isinstance(email_id, str) or _EMAIL_ID.match(email_id) is None:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    if not isinstance(message_id, str) or PLAIN_MESSAGE_ID.fullmatch(message_id) is None:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    recipients = _addresses(data.get("to")) + _addresses(data.get("received_for"))
    return QuoteMailNotice(email_id, message_id, frozenset(recipients))


def stored_payload(notice: QuoteMailNotice) -> dict[str, JsonValue]:
    """What the inbox keeps of a notice: Resend's email ID and the Message-ID, nothing else."""
    return {"email_id": notice.email_id, "message_id": notice.message_id}


class QuoteShops(Protocol):
    async def quote_shop(self) -> TenantShop | None:
        """The connected HTCO shop the quote mailbox serves (`HTCO_AUTOVA_QUOTE_ETSY_SHOP_ID`),
        or None when it is not connected."""
        ...


@dataclass(frozen=True)
class ConnectedQuoteShop:
    """The quote shop is the HTCO shop connected to the configured Etsy shop (5.3's lookup)."""

    shops: WebhookShops
    etsy_shop_id: int

    async def quote_shop(self) -> TenantShop | None:
        return await self.shops.connected_shop(self.etsy_shop_id)


def quote_mail_job(notice: QuoteMailNotice, shop: TenantShop) -> JobSpec:
    return JobSpec(
        kind=QUOTE_MAIL_JOB,
        idempotency_key=f"{QUOTE_SOURCE}:{notice.message_id}"[:200],
        payload=stored_payload(notice),
        organization_id=shop.organization_id,
        shop_id=shop.shop_id,
        correlation_id=f"{QUOTE_SOURCE}:{notice.email_id}",
    )


@dataclass
class QuoteMailboxIngress:
    secret: WebhookSecret
    inbox: WebhookInbox
    shops: QuoteShops
    mailbox: str  # our quote address, with its token: configuration, never logged
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    # Notices stored without a job, by reason (the 5.9 metrics read these).
    unrouted: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.mailbox = self.mailbox.strip().lower()

    async def receive(self, headers: Mapping[str, str], body: bytes) -> Accepted:
        now = self.clock()
        verify_signature(self.secret, standard_headers(headers), body, now=now)
        notice = parse_notice(body)
        shop = await self.shops.quote_shop()
        event = InboxEvent(
            source=QUOTE_SOURCE,
            event_id=notice.message_id,
            event_type=RECEIVED,
            payload=stored_payload(notice),
            received_at=now,
            organization_id=None if shop is None else shop.organization_id,
            shop_id=None if shop is None else shop.shop_id,
        )
        reason = None
        if self.mailbox not in notice.recipients:
            reason = "not_our_mailbox"
        elif shop is None:
            reason = "unknown_shop"
        if reason is not None or shop is None:
            inbox_id, duplicate = await self.offload(lambda: self.inbox.keep(event, now=now))
            if not duplicate:
                self.unrouted[reason or "unknown_shop"] += 1
            _log.info("autova.quote_mail_kept: %s duplicate=%s", reason, duplicate)
            return Accepted(inbox_id=inbox_id, duplicate=duplicate, job_id=None)
        recorded = await self.offload(
            lambda: self.inbox.record(event, quote_mail_job(notice, shop), now=now)
        )
        _log.info(
            "autova.quote_mail_received: shop=%s duplicate=%s", shop.shop_id, recorded.duplicate
        )
        return Accepted(recorded.inbox_id, recorded.duplicate, recorded.job_id)


# --- The parse job (the worker) ----------------------------------------------------------------


class ReceivedMails(Protocol):
    async def raw_mail(self, email_id: str) -> str:
        """The whole mail Resend received (headers and body). Raises `JobError` when it cannot
        be fetched; the job retries."""
        ...


@dataclass(frozen=True)
class QuoteMailProcessor:
    mails: ReceivedMails
    intake: QuoteIntake
    authserv_id: str  # HTCO_AUTOVA_QUOTE_AUTHSERV_ID
    sender: str  # HTCO_AUTOVA_QUOTE_SENDER

    async def process(self, job: ClaimedJob) -> IntakeOutcome:
        email_id = job.payload.get("email_id")
        if job.organization_id is None or job.shop_id is None:
            raise JobError("a quote mail job names its shop and organization")
        if not isinstance(email_id, str) or _EMAIL_ID.match(email_id) is None:
            raise JobError("a quote mail job names Resend's email ID")
        text = await self.mails.raw_mail(email_id)
        mail = QuoteMail.from_text(text, authserv_id=self.authserv_id, sender=self.sender)
        return await self.intake.handle(mail, ShopForMail(job.organization_id, job.shop_id))


# --- Human tasks "check this quote", in memory until 7.1's task store -------------------------


@dataclass
class QuoteReviewTask:
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    reason: str
    message_id: str = ""
    day: date | None = None  # a roll-up's day
    count: int = 1


@dataclass
class MemoryQuoteReviews:
    """`QuoteReviews` with the rules 7.1's task store keeps: one task per mail, and one forgery
    roll-up per shop and day carrying its count (#192)."""

    tasks: dict[tuple[Any, ...], QuoteReviewTask] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def check_this_quote(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, message_id: str, reason: str
    ) -> None:
        key = (organization_id, shop_id, "mail", message_id or uuid.uuid7().hex, reason)
        with self._lock:
            self.tasks.setdefault(
                key, QuoteReviewTask(organization_id, shop_id, reason, message_id)
            )

    async def possible_forgery(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, day: date
    ) -> None:
        key = (organization_id, shop_id, "forgery", day)
        with self._lock:
            task = self.tasks.get(key)
            if task is None:
                reason = QuarantineReason.UNAUTHENTICATED_SENDER.value
                self.tasks[key] = QuoteReviewTask(organization_id, shop_id, reason, day=day)
            else:
                task.count += 1
