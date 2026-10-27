"""Suppliers, quote requests, supplier quotes and supplier mappings (slice 7.13, moved from 13.1).

One home for the supplier model from the start (docs/architecture/autova-integration.md); Phase
13 extends it and never creates another.

- A **product** is a listing of a shop, or one variant of it (`ProductRef`).
- A **quote request** is one product waiting for a supplier:
  `WAITING_FOR_QUOTES` → `QUOTES_RECEIVED` → `CHOSEN` → `CONFIRMED_IN_AUTOVA` → `MAPPED`, or
  `CANCELLED` from any state that is not final. A choice whose Owner approval is rejected or
  expires (7.17) goes back to `QUOTES_RECEIVED`.
- A **supplier quote** is money in a currency (`Money`, never a float; AC-7.13.2), with the
  shipping method, the processing and shipping times, the validity and the source mail's
  reference. Its landed cost is computed here, in Decimal.
- A **supplier mapping** says which supplier serves a product, from when, and from which quote. A
  product has at most one active mapping: activating a new one ends the old one with a reason
  (AC-7.13.1). The database enforces the same with a partial unique index.

Identifiers, money and terms only: no buyer data, and no free text from a model.
"""

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum

from htco_domain.orders import Money
from htco_domain.state_machine import check_transition

MAX_NAME = 200
MAX_VARIANT = 120
MAX_METHOD = 120
MAX_REFERENCE = 200
MAX_DAYS = 365


def _aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")


@dataclass(frozen=True)
class ProductRef:
    """A shop's listing, or one variant of it (`variant` empty means the whole listing)."""

    shop_id: uuid.UUID
    listing_id: int
    variant: str = ""

    def __post_init__(self) -> None:
        if self.listing_id <= 0:
            raise ValueError("a product's listing ID is positive")
        if len(self.variant) > MAX_VARIANT:
            raise ValueError(f"a product's variant is at most {MAX_VARIANT} characters")

    @property
    def key(self) -> str:
        return f"{self.shop_id}:{self.listing_id}:{self.variant}"


@dataclass(frozen=True)
class Supplier:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    # How AutoVA names the supplier on its pages and mails, to match quotes and orders.
    autova_name: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("name", self.name), ("AutoVA name", self.autova_name)):
            if value is not None and not (0 < len(value.strip()) <= MAX_NAME):
                raise ValueError(f"a supplier's {label} is 1 to {MAX_NAME} characters")


class QuoteRequestState(StrEnum):
    WAITING_FOR_QUOTES = "waiting_for_quotes"
    QUOTES_RECEIVED = "quotes_received"
    CHOSEN = "chosen"
    CONFIRMED_IN_AUTOVA = "confirmed_in_autova"
    MAPPED = "mapped"
    CANCELLED = "cancelled"


_S = QuoteRequestState
QUOTE_REQUEST_TRANSITIONS: Mapping[QuoteRequestState, frozenset[QuoteRequestState]] = {
    _S.WAITING_FOR_QUOTES: frozenset({_S.QUOTES_RECEIVED, _S.CANCELLED}),
    _S.QUOTES_RECEIVED: frozenset({_S.CHOSEN, _S.CANCELLED}),
    # Back to the quotes when the Owner's approval of the choice is rejected or expires (7.17).
    _S.CHOSEN: frozenset({_S.CONFIRMED_IN_AUTOVA, _S.QUOTES_RECEIVED, _S.CANCELLED}),
    _S.CONFIRMED_IN_AUTOVA: frozenset({_S.MAPPED, _S.CANCELLED}),
    _S.MAPPED: frozenset(),
    _S.CANCELLED: frozenset(),
}
# The states that name the chosen quote.
HOLDS_A_CHOICE: frozenset[QuoteRequestState] = frozenset(
    {_S.CHOSEN, _S.CONFIRMED_IN_AUTOVA, _S.MAPPED}
)


@dataclass(frozen=True)
class QuoteRequest:
    id: uuid.UUID
    organization_id: uuid.UUID
    product: ProductRef
    state: QuoteRequestState
    updated_at: datetime
    chosen_quote_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _aware(self.updated_at, "a quote request's time")
        # The database's quote_requests_chosen_check, the same rule.
        if (self.state in HOLDS_A_CHOICE) != (self.chosen_quote_id is not None):
            raise ValueError("a request names its chosen quote from CHOSEN on, and only then")

    @property
    def is_final(self) -> bool:
        return not QUOTE_REQUEST_TRANSITIONS[self.state]

    def transition(self, target: QuoteRequestState, *, at: datetime) -> QuoteRequest:
        """Any edge but the choice; going back to the quotes, or cancelling, clears the choice."""
        check_transition("quote request", QUOTE_REQUEST_TRANSITIONS, self.state, target)
        if target is QuoteRequestState.CHOSEN:
            raise ValueError("a choice names its quote: use choose()")
        chosen = self.chosen_quote_id if target in HOLDS_A_CHOICE else None
        return replace(self, state=target, updated_at=at, chosen_quote_id=chosen)

    def choose(self, quote_id: uuid.UUID, *, at: datetime) -> QuoteRequest:
        """QUOTES_RECEIVED -> CHOSEN, naming the chosen quote (one of this request's own)."""
        check_transition(
            "quote request", QUOTE_REQUEST_TRANSITIONS, self.state, QuoteRequestState.CHOSEN
        )
        return replace(
            self, state=QuoteRequestState.CHOSEN, updated_at=at, chosen_quote_id=quote_id
        )


# A quote's source, in one of two strict shapes so no mail text can land there:
# - "autova-quote:" and AutoVA's quote ID (preferred: it stays the same when AutoVA sends the
#   quote again in a new mail);
# - "mail:" and the SHA-256 of the mail's message ID, when the mail carries no quote ID.
# The database checks the same shapes (supplier_quotes_source_reference_check).
_AUTOVA_QUOTE_ID = re.compile(r"[A-Z]{1,4}-[0-9]{1,12}")
_SOURCE_REFERENCE = re.compile(r"autova-quote:[A-Z]{1,4}-[0-9]{1,12}|mail:[0-9a-f]{64}")


def autova_quote_reference(quote_id: str) -> str:
    """The reference a quote stores for AutoVA's quote ID (for example `Q-88213`)."""
    if _AUTOVA_QUOTE_ID.fullmatch(quote_id) is None:
        raise ValueError("an AutoVA quote ID is 1 to 4 capitals, a dash and 1 to 12 digits")
    return f"autova-quote:{quote_id}"


def mail_reference(message_id: str) -> str:
    """The reference a quote stores for a mail without a quote ID: a hash of its message ID."""
    if not message_id:
        raise ValueError("a mail reference needs the mail's message ID")
    return "mail:" + hashlib.sha256(message_id.encode()).hexdigest()


def _days(window: tuple[int, int], what: str) -> None:
    low, high = window
    if not (0 <= low <= high <= MAX_DAYS):
        raise ValueError(f"{what} is a range of 0 to {MAX_DAYS} days, the shortest first")


@dataclass(frozen=True)
class SupplierQuote:
    id: uuid.UUID
    organization_id: uuid.UUID
    quote_request_id: uuid.UUID
    supplier_id: uuid.UUID
    unit_price: Money
    shipping_cost: Money
    shipping_method: str
    processing_days: tuple[int, int]
    shipping_days: tuple[int, int]
    valid_until: date | None
    # Where it came from: autova_quote_reference() or mail_reference(), never the mail's content.
    source_reference: str
    received_at: datetime

    def __post_init__(self) -> None:
        if self.unit_price.amount <= 0:
            raise ValueError("a quote's unit price is positive")
        if self.shipping_cost.amount < 0:
            raise ValueError("a quote's shipping cost is zero or more")
        if self.shipping_cost.currency != self.unit_price.currency:
            raise ValueError("a quote's price and shipping are in one currency")
        if not (0 < len(self.shipping_method.strip()) <= MAX_METHOD):
            raise ValueError(f"a quote's shipping method is 1 to {MAX_METHOD} characters")
        _days(self.processing_days, "a quote's processing time")
        _days(self.shipping_days, "a quote's shipping time")
        if (
            len(self.source_reference) > MAX_REFERENCE
            or _SOURCE_REFERENCE.fullmatch(self.source_reference) is None
        ):
            raise ValueError(
                "a quote's source reference is an AutoVA quote ID or a mail hash, never text"
            )
        _aware(self.received_at, "a quote's arrival")

    @property
    def landed_cost(self) -> Money:
        """The unit price plus the shipping, exactly (7.16 sorts the quotes by it)."""
        return Money(self.unit_price.amount + self.shipping_cost.amount, self.unit_price.currency)

    def is_valid_on(self, day: date) -> bool:
        return self.valid_until is None or day <= self.valid_until


class MappingEndReason(StrEnum):
    REPLACED = "replaced"  # another supplier was mapped (activate_mapping)
    SUPPLIER_STOPPED = "supplier_stopped"
    PRODUCT_RETIRED = "product_retired"


@dataclass(frozen=True)
class SupplierMapping:
    id: uuid.UUID
    organization_id: uuid.UUID
    product: ProductRef
    supplier_id: uuid.UUID
    quote_id: uuid.UUID | None  # the quote it came from; None for one recorded by hand
    active_from: datetime
    ended_at: datetime | None = None
    end_reason: MappingEndReason | None = None

    def __post_init__(self) -> None:
        _aware(self.active_from, "a mapping's start")
        if (self.ended_at is None) != (self.end_reason is None):
            raise ValueError("a mapping ends with a reason, and only an ended one has a reason")
        if self.ended_at is not None:
            _aware(self.ended_at, "a mapping's end")
            if self.ended_at < self.active_from:
                raise ValueError("a mapping cannot end before it starts")

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


def activate_mapping(
    current: SupplierMapping | None,
    new: SupplierMapping,
    *,
    at: datetime,
    reason: MappingEndReason = MappingEndReason.REPLACED,
) -> tuple[SupplierMapping | None, SupplierMapping]:
    """The product's one active mapping becomes `new`; the current one, if any, ends with
    `reason` at `at` (AC-7.13.1). Returns (the ended mapping or None, the new one)."""
    if not new.is_active:
        raise ValueError("only an active mapping can be activated")
    if current is None:
        return None, new
    if current.product != new.product or current.organization_id != new.organization_id:
        raise ValueError("a mapping replaces only the same product's mapping")
    if not current.is_active:
        raise ValueError("that mapping has already ended")
    if new.active_from < at:
        raise ValueError("the new mapping starts when the old one ends or later, never overlapping")
    return replace(current, ended_at=at, end_reason=reason), new
