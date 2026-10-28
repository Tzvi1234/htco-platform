"""The marketplace port (slice 4.1): what HTCO reads from a connected shop, and how it fails.

Use cases depend on `MarketplacePort`, never on Etsy's payloads; the Etsy adapter in
`htco_integrations.etsy` maps Etsy's JSON to these types and its HTTP failures to these errors.
Only the fields HTCO uses are carried. Marketplace IDs are the marketplace's own integers.

A call is *routine* unless the caller marks it `urgent`. When the marketplace's daily request
budget runs low, routine calls are deferred (`QuotaDeferredError`) so the remaining budget is
kept for urgent work such as reacting to a paid order (AC-4.1.2).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, override, runtime_checkable

# Re-exported for the locked `execution/` and integrations `etsy.py` until their follow-ups.
from htco_application.kernel import Page as Page  # noqa: PLC0414
from htco_domain.orders import OrderState

_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class MarketplaceMoney:
    """An exact amount as the marketplace sends it: `amount / divisor` in `currency_code`."""

    amount: int
    divisor: int
    currency_code: str

    def __post_init__(self) -> None:
        if self.divisor <= 0:
            raise ValueError("money divisor must be positive")
        if not _CURRENCY.fullmatch(self.currency_code):
            raise ValueError("money currency must be an ISO 4217 code such as USD")

    def as_decimal(self) -> Decimal:
        """The exact value; the scale follows the divisor (2599 / 100 is `Decimal("25.99")`)."""
        exponent = len(str(self.divisor)) - 1
        if self.divisor == 10**exponent:
            return Decimal(self.amount).scaleb(-exponent)
        return Decimal(self.amount) / Decimal(self.divisor)


@dataclass(frozen=True)
class HolidayPreference:
    """One holiday of the shop's country and whether the shop works on it (6.9): the business-day
    calendar's holidays, and what `etsy.holiday_preferences.update` changes."""

    holiday_id: int
    country_iso: str
    is_working: bool
    name: str


@dataclass(frozen=True)
class MarketplaceShop:
    shop_id: int
    name: str
    currency_code: str
    url: str | None
    is_vacation: bool
    active_listing_count: int
    sold_transaction_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReceiptLine:
    transaction_id: int
    # None when the listing no longer exists.
    listing_id: int | None
    sku: str | None
    quantity: int
    unit_price: MarketplaceMoney


@dataclass(frozen=True)
class Receipt:
    """An order as the marketplace reports it. Buyer details are deliberately not carried."""

    receipt_id: int
    # Lower-case marketplace status, for example "paid", "completed" or "canceled".
    status: str
    is_paid: bool
    is_shipped: bool
    created_at: datetime
    updated_at: datetime
    grand_total: MarketplaceMoney
    lines: tuple[ReceiptLine, ...]


class ListingState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SOLD_OUT = "sold_out"
    DRAFT = "draft"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Listing:
    listing_id: int
    title: str
    state: ListingState
    quantity: int
    price: MarketplaceMoney
    url: str | None
    updated_at: datetime


# Webhook re-reads (slice 5.3): a listing's state and its stock, as Etsy reports them now.


@dataclass(frozen=True)
class ListingSnapshot:
    """A listing as `getListing` reports it, with the shop it belongs to."""

    listing_id: int
    etsy_shop_id: int
    state: str
    quantity: int
    updated_at: datetime


@dataclass(frozen=True)
class ProductStock:
    product_id: int
    quantity: int  # enabled offerings, summed


@dataclass(frozen=True)
class InventorySnapshot:
    listing_id: int
    products: tuple[ProductStock, ...]

    @property
    def total_quantity(self) -> int:
        return sum(p.quantity for p in self.products)


@dataclass(frozen=True)
class ReceiptAddressCheck:
    """Whether the newest receipt carries the buyer's shipping address (slice 4.5).

    Presence only: the address itself is never read into HTCO, stored or logged. Etsy hides the
    address fields for some new keys and countries (2026-07-20 restriction); this tells the shop
    card whether it does so for this shop. `address_present` is None when there is no receipt.
    """

    receipts_seen: int
    address_present: bool | None


# --- What the dashboard shows (read through, never stored; see shop_dashboard) -------------------


@dataclass(frozen=True)
class ShopProfile:
    """The shop as its public page and the seller see it: identity, counts and reputation."""

    shop_id: int
    name: str
    title: str | None
    url: str | None
    currency_code: str
    icon_url: str | None
    banner_url: str | None
    is_vacation: bool
    active_listing_count: int
    digital_listing_count: int
    sold_transaction_count: int
    favorer_count: int
    # None until the shop has reviews.
    review_average: Decimal | None
    review_count: int | None
    created_at: datetime
    ships_from_country: str | None
    location_country: str | None
    languages: tuple[str, ...]
    accepts_custom_requests: bool


@dataclass(frozen=True)
class ListingImage:
    # About 170 by 135 pixels, for lists and strips.
    thumbnail_url: str
    # 570 pixels wide, for cards.
    url: str
    alt_text: str | None


@dataclass(frozen=True)
class ListingDetails:
    """A listing with what the Products screen shows. Views and favourites are Etsy's counts."""

    listing_id: int
    title: str
    state: ListingState
    quantity: int
    price: MarketplaceMoney
    url: str | None
    image: ListingImage | None
    views: int
    favorer_count: int
    tags: tuple[str, ...]
    section_id: int | None
    is_digital: bool
    is_personalizable: bool
    has_variations: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ShopSection:
    section_id: int
    title: str
    rank: int
    active_listing_count: int


@dataclass(frozen=True)
class OrderLine:
    transaction_id: int
    listing_id: int | None
    title: str
    sku: str | None
    quantity: int
    unit_price: MarketplaceMoney
    is_digital: bool
    # When Etsy expects the item to ship (processing time after payment); None when unknown.
    expected_ship_date: datetime | None


@dataclass(frozen=True)
class OrderShipment:
    carrier: str
    # A tracking number can locate the parcel and so the buyer: never in a repr or a log.
    tracking_code: str = field(repr=False)
    notified_at: datetime


def display_name(full_name: str | None) -> str | None:
    """First name and last initial ("Dana L."): all the Orders screen needs to tell buyers
    apart."""
    if not isinstance(full_name, str):
        return None
    words = full_name.split()
    if not words:
        return None
    if len(words) == 1:
        return words[0]
    return f"{words[0]} {words[-1][0]}."


@dataclass(frozen=True, repr=False)
class ShippingAddress:
    """Where the order ships, as the marketplace shares it with the seller (the owner's decision
    of 2026-10-22: stored for order statistics and later insights). Personal data: never in a
    repr, a log line, an error, an error report or an audit record."""

    first_line: str | None
    second_line: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country_code: str | None

    @override
    def __repr__(self) -> str:
        return "ShippingAddress(<hidden>)"


@dataclass(frozen=True)
class OrderRefund:
    """One refund of an order, as Etsy lists it on the receipt. `note_from_issuer` is the
    seller's own words: stored and shown to buyer-data roles, never in a repr or a log."""

    amount: MarketplaceMoney
    created_at: datetime
    reason: str | None
    status: str | None
    note_from_issuer: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OrderReceipt:
    """An order as the Orders screen shows it and the order history stores it.

    Contact data is never read: no email address, phone number, message, gift message or note.
    The buyer's name (`buyer_name`) and shipping address (`shipping_address`) are kept for the
    stored order history (the owner's decision of 2026-10-22); the Orders screen shows only a
    display name made of the first name and the last initial (`buyer_display_name`). None of
    them is ever in a repr or a log. `country_code` is the destination country, used for
    aggregate statistics.
    """

    receipt_id: int
    # Lower-case marketplace status: "paid", "completed", "open", "payment processing",
    # "canceled", "fully refunded" or "partially refunded".
    status: str
    is_paid: bool
    is_shipped: bool
    created_at: datetime
    updated_at: datetime
    grand_total: MarketplaceMoney
    subtotal: MarketplaceMoney
    shipping_total: MarketplaceMoney
    discount_total: MarketplaceMoney
    lines: tuple[OrderLine, ...]
    shipments: tuple[OrderShipment, ...]
    buyer_display_name: str | None = field(default=None, repr=False)
    # The destination country (ISO 3166-1 alpha-2), for aggregate statistics.
    country_code: str | None = field(default=None, repr=False)
    # The full name the order ships to, and the shipping address: personal data, stored only in
    # the order history's buyer table.
    buyer_name: str | None = field(default=None, repr=False)
    shipping_address: ShippingAddress | None = field(default=None, repr=False)
    # Refunds with their reason, oldest first (empty when none).
    refunds: tuple[OrderRefund, ...] = ()
    # The order state machine's state as stored (FR-ORD-03); None for a receipt read live.
    stored_state: OrderState | None = None


@dataclass(frozen=True)
class ShopReview:
    """A buyer's public review. The text is the buyer's words: never in a repr or a log."""

    listing_id: int
    rating: int
    language: str | None
    created_at: datetime
    text: str | None = field(default=None, repr=False)
    # The order line it reviews (getReviewsByShop's `transaction_id`); links the order (6.9).
    transaction_id: int | None = None


@dataclass(frozen=True)
class ShippingDestination:
    # None with a region (for example "eu" or "non_eu") instead of one country.
    country_code: str | None
    region: str | None
    first_item_cost: MarketplaceMoney
    additional_item_cost: MarketplaceMoney
    min_delivery_days: int | None
    max_delivery_days: int | None
    mail_class: str | None


@dataclass(frozen=True)
class ShippingProfile:
    profile_id: int
    title: str | None
    origin_country: str
    # "manual" or "calculated".
    profile_type: str
    destinations: tuple[ShippingDestination, ...]
    upgrade_count: int


@dataclass(frozen=True)
class ReturnPolicy:
    policy_id: int
    accepts_returns: bool
    accepts_exchanges: bool
    # Days the buyer has to return; None when returns are not accepted.
    return_deadline_days: int | None


@dataclass(frozen=True)
class ProductionPartner:
    partner_id: int
    name: str
    location: str


@dataclass(frozen=True)
class QuotaSnapshot:
    """The request budget the marketplace reported on its latest answer (None: not reported)."""

    limit_per_second: int | None
    remaining_this_second: int | None
    limit_per_day: int | None
    remaining_today: int | None
    observed_at: datetime

    def is_below(self, floor: int) -> bool:
        return self.remaining_today is not None and self.remaining_today < floor


@dataclass(frozen=True)
class QuotaStatus:
    """The marketplace's daily request budget for the shop card (AC-4.1.2): the latest snapshot
    this process saw (None before its first call; the budget is the app's, not one shop's) and
    the floor below which routine calls are deferred."""

    snapshot: QuotaSnapshot | None
    floor: int

    @property
    def below_floor(self) -> bool:
        return self.snapshot is not None and self.snapshot.is_below(self.floor)


class QuotaMonitor(Protocol):
    """The adapter's view of its request budget (`htco_integrations.etsy.EtsyAdapter`)."""

    def quota_status(self) -> QuotaStatus: ...


# --- Errors -----------------------------------------------------------------------------------


class MarketplaceError(Exception):
    """A marketplace call failed. `status` is the HTTP status, or None without an answer.

    Messages name the operation and status only: never a token, key, URL query or payload.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MarketplaceAuthError(MarketplaceError):
    """401: the access token is missing, expired or revoked; refresh or reconnect."""


class MarketplaceCredentialsRejectedError(MarketplaceAuthError):
    """The marketplace refused the connection's credentials themselves: the token is not valid
    for this app (401 `invalid_token`), or the app's key is not accepted (a 403 about the API
    key). Retrying or refreshing cannot help; the owner must reconnect. `reason` is a short
    lower-case code (`invalid_token`, `api_key_rejected`), never payload text."""

    def __init__(self, message: str, *, status: int | None = None, reason: str) -> None:
        super().__init__(message, status=status)
        self.reason = reason


class MarketplacePermissionError(MarketplaceError):
    """403: the app or the granted scopes do not allow this call (a capability is missing)."""


class MarketplaceNotFoundError(MarketplaceError):
    """404: the shop, receipt or listing does not exist for this token."""


class MarketplaceConflictError(MarketplaceError):
    """409: the resource changed concurrently; re-read before trying again."""


class MarketplaceRequestError(MarketplaceError):
    """Another 4xx: the request itself is wrong. Retrying it unchanged cannot help."""


class MarketplaceRateLimitedError(MarketplaceError):
    """429 after the retries allowed: wait `retry_after_seconds` (None: unknown)."""

    def __init__(self, message: str, *, retry_after_seconds: float | None) -> None:
        super().__init__(message, status=429)
        self.retry_after_seconds = retry_after_seconds


class MarketplaceUnavailableError(MarketplaceError):
    """5xx or no answer at all (timeout, connection failure) after the retries allowed."""


class MarketplaceResponseError(MarketplaceError):
    """The marketplace answered successfully but with a payload we cannot read."""


class MarketplaceWriteRefusedError(MarketplaceError):
    """A request that could change shop data was stopped before it left HTCO.

    HTCO only reads from marketplaces (the owner's rule for live shops with real buyers): any
    method but GET towards the shop API is refused unless a caller passes an explicit per-call
    allow flag, which no code path does today.
    """


class QuotaDeferredError(MarketplaceError):
    """A routine call was not made because the daily request budget is below the floor."""

    def __init__(self, message: str, *, remaining_today: int, floor: int) -> None:
        super().__init__(message)
        self.remaining_today = remaining_today
        self.floor = floor


# --- Ports ------------------------------------------------------------------------------------


class AccessTokenProvider(Protocol):
    """Supplies a valid OAuth access token for a marketplace shop (the token vault, slice 4.2)."""

    async def access_token(self, shop_id: int) -> str: ...


@runtime_checkable
class CredentialsRejectionSink(Protocol):
    """An `AccessTokenProvider` that is told when the marketplace refused its credentials
    (`MarketplaceCredentialsRejectedError`), so the connection is marked `needs_reauth` at once
    instead of failing call after call while it still looks connected."""

    async def credentials_rejected(self, shop_id: int, *, reason: str) -> None: ...


class MarketplacePort(Protocol):
    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop: ...

    async def list_receipts(  # noqa: PLR0913 - each keyword is one filter of the same query
        self,
        shop_id: int,
        *,
        min_last_modified: datetime | None = None,
        max_last_modified: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Receipt]: ...

    async def list_listings(
        self,
        shop_id: int,
        *,
        state: ListingState = ListingState.ACTIVE,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Listing]: ...

    def last_seen_quota(self) -> QuotaSnapshot | None:
        """The latest reported request budget, for the shop card (None before any call)."""
        ...
