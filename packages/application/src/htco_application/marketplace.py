"""The marketplace port (slice 4.1): what HTCO reads from a connected shop, and how it fails.

Use cases depend on `MarketplacePort`, never on Etsy's payloads; the Etsy adapter in
`htco_integrations.etsy` maps Etsy's JSON to these types and its HTTP failures to these errors.
Only the fields HTCO uses are carried. Marketplace IDs are the marketplace's own integers.

A call is *routine* unless the caller marks it `urgent`. When the marketplace's daily request
budget runs low, routine calls are deferred (`QuotaDeferredError`) so the remaining budget is
kept for urgent work such as reacting to a paid order (AC-4.1.2).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

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


@dataclass(frozen=True)
class ReceiptAddressCheck:
    """Whether the newest receipt carries the buyer's shipping address (slice 4.5).

    Presence only: the address itself is never read into HTCO, stored or logged. Etsy hides the
    address fields for some new keys and countries (2026-07-20 restriction); this tells the shop
    card whether it does so for this shop. `address_present` is None when there is no receipt.
    """

    receipts_seen: int
    address_present: bool | None


@dataclass(frozen=True)
class Page[ItemT]:
    """One page of an offset-paginated list."""

    items: tuple[ItemT, ...]
    # How many items match the query in total, on every page.
    total_count: int
    offset: int

    @property
    def next_offset(self) -> int | None:
        following = self.offset + len(self.items)
        return following if self.items and following < self.total_count else None


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
