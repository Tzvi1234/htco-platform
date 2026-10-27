"""The "New product quotes" board and its notification (slice 7.16).

**The board.** One card per product waiting for a supplier (a quote request that is
`waiting_for_quotes` or `quotes_received`), with its quotes side by side: supplier, unit price,
shipping cost, the total landed cost, processing and shipping times, and validity. The total is
computed here in `Decimal`, never in the screen (AC-7.16.2). Quotes are sorted by total, cheapest
first; a quote past its validity is not choosable and goes last (the screen greys it out). Quotes
in different currencies are never compared: they sort by currency, then by total.

Only the Owner and Admins read it (`Capability.VIEW_SUPPLIER_QUOTES`), the people who choose a
quote (7.17). The read runs as the signed-in user, so row-level security applies as well.

**The notification.** A new product's quote raises `new_product_quote` for the owner and admins:
in-app at once, plus an email unless they turned it off (`NotificationService`). It is keyed by
the request and the quote's source reference, so the same mail again, or a retried job, notifies
once. A quote for a product that already has a supplier (an alternative) raises none (AC-7.15.3).
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, ShopRef, decide
from htco_application.kernel import ShopOwners
from htco_application.notifications import NotificationRequest, NotificationService
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_application.suppliers import NewQuote
from htco_domain.notifications import NotificationKind

OPEN_STATES = ("waiting_for_quotes", "quotes_received")


@dataclass(frozen=True)
class QuoteRow:
    """One open request and, when it has any, one of its quotes (as the reader returns them)."""

    request_id: uuid.UUID
    listing_id: int
    variant: str
    state: str
    requested_at: datetime
    quote_id: uuid.UUID | None = None
    supplier: str | None = None
    currency: str | None = None
    unit_price: Decimal | None = None
    shipping_cost: Decimal | None = None
    shipping_method: str | None = None
    processing_days: tuple[int, int] | None = None
    shipping_days: tuple[int, int] | None = None
    valid_until: date | None = None
    received_at: datetime | None = None


@dataclass(frozen=True)
class QuoteCard:
    id: uuid.UUID
    supplier: str
    currency: str
    unit_price: Decimal
    shipping_cost: Decimal
    total: Decimal  # unit price plus shipping, in Decimal
    shipping_method: str
    processing_days: tuple[int, int]
    shipping_days: tuple[int, int]
    valid_until: date | None
    received_at: datetime
    choosable: bool  # False once past its validity: greyed out, cannot be chosen


@dataclass(frozen=True)
class ProductQuotes:
    request_id: uuid.UUID
    listing_id: int
    variant: str
    state: str
    requested_at: datetime
    quotes: tuple[QuoteCard, ...]


def landed_total(unit_price: Decimal, shipping_cost: Decimal) -> Decimal:
    return unit_price + shipping_cost


def _card(row: QuoteRow, today: date) -> QuoteCard | None:
    if (
        row.quote_id is None or row.supplier is None or row.currency is None
        or row.unit_price is None or row.shipping_cost is None or row.shipping_method is None
        or row.processing_days is None or row.shipping_days is None or row.received_at is None
    ):  # fmt: skip
        return None
    return QuoteCard(
        id=row.quote_id,
        supplier=row.supplier,
        currency=row.currency,
        unit_price=row.unit_price,
        shipping_cost=row.shipping_cost,
        total=landed_total(row.unit_price, row.shipping_cost),
        shipping_method=row.shipping_method,
        processing_days=row.processing_days,
        shipping_days=row.shipping_days,
        valid_until=row.valid_until,
        received_at=row.received_at,
        choosable=row.valid_until is None or today <= row.valid_until,
    )


def _order(card: QuoteCard) -> tuple[bool, str, Decimal, datetime, uuid.UUID]:
    return (not card.choosable, card.currency, card.total, card.received_at, card.id)


def quote_board(rows: Sequence[QuoteRow], *, today: date) -> list[ProductQuotes]:
    """The cards, oldest request first, each with its quotes sorted by total."""
    requests: dict[uuid.UUID, QuoteRow] = {}
    cards: dict[uuid.UUID, list[QuoteCard]] = {}
    for row in rows:
        requests.setdefault(row.request_id, row)
        card = _card(row, today)
        bucket = cards.setdefault(row.request_id, [])
        if card is not None:
            bucket.append(card)
    ordered = sorted(requests.values(), key=lambda r: (r.requested_at, r.request_id))
    return [
        ProductQuotes(
            request_id=r.request_id,
            listing_id=r.listing_id,
            variant=r.variant,
            state=r.state,
            requested_at=r.requested_at,
            quotes=tuple(sorted(cards[r.request_id], key=_order)),
        )
        for r in ordered
    ]


class QuoteBoardReader(Protocol):
    def open_quotes(self, claims: Mapping[str, Any], shop_id: uuid.UUID) -> list[QuoteRow]:
        """The shop's open requests with their quotes and suppliers' names, as the user."""
        ...


def read_quote_board(  # noqa: PLR0913 - the request's parts
    auth: AuthContext,
    reader: QuoteBoardReader,
    shops: ShopOwners,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    today: Callable[[datetime], date] = lambda at: at.date(),
) -> list[ProductQuotes]:
    """A shop the caller cannot see does not exist (`ShopNotFoundError`); a member who may see
    the shop but not its quotes is refused (`ShopAccessDeniedError`)."""
    organization = shops.organization_of_shop(shop_id)
    if organization is None:
        raise ShopNotFoundError("no such shop")
    try:
        principal = auth.principal_for(organization)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop") from None
    shop = ShopRef(id=shop_id, organization_id=organization)
    if decide(principal, Capability.VIEW_OVERVIEW, shop=shop, now=now).outcome is not Outcome.ALLOW:
        raise ShopNotFoundError("no such shop")
    decision = decide(principal, Capability.VIEW_SUPPLIER_QUOTES, shop=shop, now=now)
    if decision.outcome is not Outcome.ALLOW:
        raise ShopAccessDeniedError("only the Owner and Admins see supplier quotes")
    return quote_board(reader.open_quotes(auth.claims, shop_id), today=today(now))


# --- The notification -----------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationQuoteAlerts:
    service: NotificationService

    async def new_product_quote(self, quote: NewQuote) -> None:
        await self.service.notify(
            NotificationRequest(
                organization_id=quote.organization_id,
                kind=NotificationKind.NEW_PRODUCT_QUOTE,
                dedup_key=f"quote:{quote.quote_request_id}:{quote.source_reference}"[:120],
                params={
                    "listing_id": quote.listing_id,
                    "quote_request_id": str(quote.quote_request_id),
                },
                shop_id=quote.shop_id,
                correlation_id=f"quote-request:{quote.quote_request_id}",
            )
        )
