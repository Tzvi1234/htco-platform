"""The quote intake (slice 7.15): an AutoVA quote mail becomes a supplier quote, or a Human task.

`QuoteIntake.handle` takes one mail from the quote mailbox (7.14's inbox job hands it over, with
the shop the mailbox serves) and:

1. parses it with the deterministic `QuoteParser` (no model, AC-7.15.2);
2. a quarantined mail records nothing. Another sender opens no task; a mail whose `From` names
   AutoVA but which our receiving server did not see signed by AutoVA's domain (an aligned DKIM
   pass) is counted as a possible forgery in one Human task per shop per day (#192);
3. a refused mail (not the known layout) opens one Human task "check this quote", naming the mail
   and the reason; nothing is guessed (AC-7.15.1);
4. a parsed quote whose DKIM-signed `To` is not the owner's mailbox may be another AutoVA
   customer's mail, replayed to us (#192): one Human task `unknown_listing`, nothing recorded;
5. a parsed quote is recorded in one transaction: its supplier (found by the name AutoVA shows, or
   added), its product's open quote request (created for the product's first quote, moved to
   `quotes_received`) and the quote, stored once per request and AutoVA quote (AC-7.14.1). A
   product that already has an active supplier mapping gets the quote as an alternative and no
   "new product" alert (AC-7.15.3); the alert itself is slice 7.16's.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from htco_application.kernel import Offload, run_inline
from htco_application.quote_parser import (
    PLAIN_MESSAGE_ID,
    ParsedQuote,
    Quarantined,
    QuoteMail,
    QuoteParser,
)
from htco_domain.suppliers import ProductRef, SupplierQuote


class IntakeOutcome(StrEnum):
    NEW_PRODUCT_QUOTE = "new_product_quote"  # 7.16 shows it and notifies the owner
    ALTERNATIVE = "alternative"  # the product already has a supplier: no new-product alert
    ALREADY_RECORDED = "already_recorded"  # the same quote mail again
    NEEDS_REVIEW = "needs_review"  # one Human task "check this quote"
    QUARANTINED = "quarantined"  # not from the configured AutoVA sender, or not proven to be


@dataclass(frozen=True)
class ShopForMail:
    """The organization and shop the quote mailbox serves (7.14's configuration)."""

    organization_id: uuid.UUID
    shop_id: uuid.UUID


@dataclass(frozen=True)
class RecordedQuote:
    quote_request_id: uuid.UUID
    supplier_id: uuid.UUID
    created: bool  # False: this quote was already stored
    alternative: bool  # the product had an active mapping


class SupplierQuoteStore(Protocol):
    """Synchronous (the worker's pool); run through `offload`."""

    def record(
        self, organization_id: uuid.UUID, product: ProductRef, supplier_name: str,
        quote: SupplierQuote, *, now: datetime,
    ) -> RecordedQuote:  # fmt: skip
        """In one transaction: the supplier (by its AutoVA name, added when new), the product's
        open quote request (created when none is open, set to `quotes_received`), and the quote
        unless the request has its `source_reference`. `quote`'s request and supplier IDs are
        placeholders; the store uses the ones it found or created."""
        ...


class QuoteReviews(Protocol):
    async def check_this_quote(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, message_id: str, reason: str
    ) -> None:
        """Opens one Human task "check this quote" for the mail (7.1's tasks; idempotent per
        mail). The mail itself is shown only in the task's sandboxed view."""
        ...

    async def possible_forgery(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, day: date
    ) -> None:
        """Counts one mail whose `From` names AutoVA but that our server did not see signed by
        AutoVA (`unauthenticated_sender`): one Human task per shop and day carries the count, so
        a flood of forgeries cannot bury the queue (#192). Nothing of the mail is kept."""
        ...


# The Human task's reason for a quote whose signed `To` is not the owner's mailbox (#192): it
# may be another AutoVA customer's quote, replayed to us.
UNKNOWN_LISTING = "unknown_listing"


def result_message_id(mail: QuoteMail) -> str:
    """The mail's Message-ID for its Human task, only when it reads as a plain one."""
    return mail.message_id if PLAIN_MESSAGE_ID.fullmatch(mail.message_id) else ""


def _supplier_quote(
    parsed: ParsedQuote, organization_id: uuid.UUID, now: datetime
) -> SupplierQuote:
    return SupplierQuote(
        id=uuid.uuid7(),
        organization_id=organization_id,
        quote_request_id=uuid.UUID(int=0),  # the store's
        supplier_id=uuid.UUID(int=0),  # the store's
        unit_price=parsed.unit_price,
        shipping_cost=parsed.shipping_cost,
        shipping_method=parsed.shipping_method,
        processing_days=parsed.processing_days,
        shipping_days=parsed.shipping_days,
        valid_until=parsed.valid_until,
        source_reference=parsed.source_reference,
        received_at=now,
    )


@dataclass(frozen=True)
class QuoteIntake:
    parser: QuoteParser
    store: SupplierQuoteStore
    reviews: QuoteReviews
    clock: Callable[[], datetime]
    # The owner's mailbox, the one AutoVA writes to (`HTCO_AUTOVA_QUOTE_OWNER_MAILBOX`): a quote
    # is recorded only when the sender's DKIM signature covers a `To` naming it (#192).
    owner_mailbox: str
    offload: Offload = run_inline

    def __post_init__(self) -> None:
        mailbox = self.owner_mailbox.strip().lower()
        if "@" not in mailbox or any(c.isspace() or c in "<>" for c in mailbox):
            raise ValueError("the owner's mailbox must be an email address")
        object.__setattr__(self, "owner_mailbox", mailbox)

    async def handle(self, mail: QuoteMail, target: ShopForMail) -> IntakeOutcome:
        result = self.parser.parse(mail)
        if isinstance(result, Quarantined):
            if result.needs_review:
                # A forgery flood must not bury the queue (#192): one task per shop per day.
                await self.reviews.possible_forgery(
                    target.organization_id, target.shop_id, day=self.clock().date()
                )
            return IntakeOutcome.QUARANTINED
        if isinstance(result, ParsedQuote) and self.owner_mailbox not in mail.signed_recipients:
            # A genuine AutoVA mail replayed to our mailbox (#192): its signed To is not the
            # owner's, so it may be someone else's quote. Nothing is recorded.
            await self.reviews.check_this_quote(
                target.organization_id,
                target.shop_id,
                message_id=result_message_id(mail),
                reason=UNKNOWN_LISTING,
            )
            return IntakeOutcome.NEEDS_REVIEW
        if not isinstance(result, ParsedQuote):
            await self.reviews.check_this_quote(
                target.organization_id,
                target.shop_id,
                message_id=result.message_id,
                reason=result.reason.value,
            )
            return IntakeOutcome.NEEDS_REVIEW
        now = self.clock()
        product = ProductRef(target.shop_id, result.listing_id, result.variant)
        quote = _supplier_quote(result, target.organization_id, now)
        recorded = await self.offload(
            lambda: self.store.record(
                target.organization_id, product, result.supplier, quote, now=now
            )
        )
        if not recorded.created:
            return IntakeOutcome.ALREADY_RECORDED
        if recorded.alternative:
            return IntakeOutcome.ALTERNATIVE
        return IntakeOutcome.NEW_PRODUCT_QUOTE
