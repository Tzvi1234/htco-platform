"""The connected shop's live data on the dashboard: overview, orders, listings, reviews, shipping.

Read through, never stored (Phase 4, "dashboard data"): every value comes from the marketplace
through the read-only adapter and the short per-shop cache (`shop_data_cache`), with the time it
was fetched and whether it is stale. Phase 5 ingests orders exactly once into HTCO's own store;
then the orders source changes (`DataSource.ETSY_LIVE` becomes the store) while these results and
the API contract stay the same.

Who sees what (PERMISSION_MATRIX): overview, listings, reviews and shipping need `view_overview`;
orders need `view_orders`. A buyer's display name and tracking numbers are shown only to roles
with `view_buyer_contact` and an unmasked `view_orders` (Owner, Admin, Operations, Support);
everyone else gets the order without them. A shop the caller may not see does not exist for them.

Nothing here logs, audits or raises with buyer data.
"""

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, ShopRef, decide
from htco_application.marketplace import (
    AccessTokenProvider,
    ListingDetails,
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceMoney,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
    OrderLine,
    OrderReceipt,
    Page,
    ProductionPartner,
    ReturnPolicy,
    ShippingProfile,
    ShopProfile,
    ShopReview,
    ShopSection,
)
from htco_application.shop_capabilities import ShopNotConnectedError, TokenPinning
from htco_application.shop_data_cache import CacheKey, CachePolicy, Fetched, ShopDataCache
from htco_application.shop_integrations import ShopIntegration
from htco_application.shops import ShopNotFoundError
from htco_domain.integrations import IntegrationStatus

_log = logging.getLogger(__name__)

# Sales (the overview, Home) cover the last 30 days; the Orders screen chooses a period.
SALES_WINDOW_DAYS = 30
# Pages of 100 read per load at most: 300 listings; receipts by period (RECEIPT_CAPS). Beyond
# that the result says `partial` / `truncated`.
MAX_LISTING_PAGES = 3
PAGE = 100


class OrderPeriod(StrEnum):
    """The Orders screen's periods. The longer ones read Etsy page by page, one page after the
    other, up to a cap (`RECEIPT_CAPS`)."""

    DAYS_30 = "30d"
    DAYS_90 = "90d"
    MONTHS_12 = "12m"
    ALL = "all"


# Days back for the periods counted in days; 12 months are calendar months (`_period_start`).
_PERIOD_DAYS: dict[OrderPeriod, int] = {
    OrderPeriod.DAYS_30: SALES_WINDOW_DAYS,
    OrderPeriod.DAYS_90: 90,
}
# Receipts read per load at most (pages of 100): up to 20 sequential calls for the long periods,
# well inside Etsy's 10 a second and 10,000 a day.
RECEIPT_CAPS: dict[OrderPeriod, int] = {
    OrderPeriod.DAYS_30: 500,
    OrderPeriod.DAYS_90: 1000,
    OrderPeriod.MONTHS_12: 2000,
    OrderPeriod.ALL: 2000,
}
# The cache resource of each period's receipts ("receipts" is the 30-day window the overview
# shares).
_RECEIPTS: dict[OrderPeriod, str] = {
    OrderPeriod.DAYS_30: "receipts",
    OrderPeriod.DAYS_90: "receipts:90d",
    OrderPeriod.MONTHS_12: "receipts:12m",
    OrderPeriod.ALL: "receipts:all",
}
# The statistics' basis: sale facts of the whole history, without buyer data.
_STATS = "stats"
REVIEW_ITEMS = 25
TOP_LISTINGS = 5
MAX_UTC_OFFSET_MINUTES = 14 * 60

# Etsy's API Terms: no listing data older than 6 hours, no other data older than 24 hours.
# Receipts carry buyer names: they may stand in for one hour at most.
POLICIES = {
    "profile": CachePolicy(ttl=timedelta(minutes=5), max_stale=timedelta(hours=6)),
    "listings": CachePolicy(ttl=timedelta(minutes=5), max_stale=timedelta(hours=6)),
    "receipts": CachePolicy(ttl=timedelta(minutes=2), max_stale=timedelta(hours=1)),
    # Longer periods change little within minutes and cost up to 20 calls a load; they carry
    # buyer names too, so they also leave memory after one hour.
    "receipts:90d": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=1)),
    "receipts:12m": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=1)),
    "receipts:all": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=1)),
    # The statistics' basis holds no buyer data (time, totals, items, country), so it is kept
    # longer: fresh for 6 hours, standing in up to Etsy's 24 hours.
    _STATS: CachePolicy(ttl=timedelta(hours=6), max_stale=timedelta(hours=24)),
    "reviews": CachePolicy(ttl=timedelta(minutes=15), max_stale=timedelta(hours=6)),
    "shipping": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=6)),
}

# Receipt statuses that are not a sale.
_NOT_A_SALE = frozenset({"canceled", "fully refunded"})


class DataSource(StrEnum):
    """Where a result comes from. Phase 5 adds the ingested store for orders."""

    ETSY_LIVE = "etsy_live"


class OrderState(StrEnum):
    UNPAID = "unpaid"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class OrderFilter(StrEnum):
    """The Orders screen's filters: every state, plus paid orders past their ship-by date."""

    UNPAID = "unpaid"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELED = "canceled"
    REFUNDED = "refunded"
    OVERDUE = "overdue"


# --- Errors ------------------------------------------------------------------------------------


class ShopDataUnavailableError(Exception):
    """The marketplace did not answer usefully and nothing recent enough is cached."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("the shop's live data is unavailable right now")
        self.retry_after_seconds = retry_after_seconds


class ShopDataScopeMissingError(Exception):
    """The connection's granted scopes do not allow this read; reconnecting grants them."""


# --- Ports -------------------------------------------------------------------------------------


class DashboardConnections(Protocol):
    """The part of the token vault the dashboard uses: the connection without its tokens."""

    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...


class SuccessRecorder(Protocol):
    async def record_successful_call(
        self, shop_id: uuid.UUID, *, at: datetime, generation: int | None = None
    ) -> None:
        """With `generation`: only while that connection is the connected one."""
        ...


class ShopDataSource(Protocol):
    """The marketplace reads the dashboard makes (the Etsy adapter), each with the token source
    of the connection it is made for."""

    async def get_shop_profile(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> ShopProfile: ...

    async def list_listing_details(
        self,
        shop_id: int,
        *,
        limit: int = PAGE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ListingDetails]: ...

    async def list_sections(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShopSection, ...]: ...

    async def list_order_receipts(  # noqa: PLR0913 - the adapter's read and its token source
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        limit: int = PAGE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[OrderReceipt]: ...

    async def list_reviews(
        self,
        shop_id: int,
        *,
        limit: int = PAGE,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[ShopReview]: ...

    async def list_shipping_profiles(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ShippingProfile, ...]: ...

    async def list_return_policies(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ReturnPolicy, ...]: ...

    async def list_production_partners(
        self, shop_id: int, *, urgent: bool = False, tokens: AccessTokenProvider | None = None
    ) -> tuple[ProductionPartner, ...]: ...


@dataclass(frozen=True)
class ShopDashboard:
    """The dashboard's collaborators, built by the composition root."""

    connections: DashboardConnections
    source: ShopDataSource
    tokens: TokenPinning
    cache: ShopDataCache
    # Moves the shop card's "last synced" when a load succeeds; optional.
    successes: SuccessRecorder | None = None


# --- Results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Freshness:
    source: DataSource
    # When the oldest part of the result was fetched from the marketplace.
    fetched_at: datetime
    # True: the marketplace did not answer and older cached data stands in.
    stale: bool


@dataclass(frozen=True)
class SalesPeriod:
    orders: int
    units: int
    revenue: Decimal


@dataclass(frozen=True)
class DailySales:
    day: date
    orders: int
    revenue: Decimal


@dataclass(frozen=True)
class TopListing:
    listing_id: int
    title: str
    units: int
    revenue: Decimal


@dataclass(frozen=True)
class SalesSummary:
    """Paid orders that were not cancelled or fully refunded, by the day they were placed in the
    caller's time zone. Revenue is the orders' grand total (items, shipping and tax, before
    Etsy's fees) in the shop's currency."""

    currency: str
    today: SalesPeriod
    last_7_days: SalesPeriod
    last_30_days: SalesPeriod
    # Oldest first, one per day of the window, today last.
    daily: tuple[DailySales, ...]
    awaiting_shipment: int
    overdue: int
    top_listings: tuple[TopListing, ...]
    # True: the shop had more orders in the window than one load reads.
    partial: bool


@dataclass(frozen=True)
class ShopOverview:
    shop_id: uuid.UUID
    freshness: Freshness
    profile: ShopProfile
    # None when the orders could not be read (for example a scope is missing).
    sales: SalesSummary | None


@dataclass(frozen=True)
class OrderItem:
    listing_id: int | None
    title: str
    quantity: int
    unit_price: Money
    sku: str | None
    is_digital: bool


@dataclass(frozen=True)
class OrderShipmentView:
    carrier: str
    shipped_at: datetime
    # Only for roles that may see buyer contact details.
    tracking_code: str | None


@dataclass(frozen=True)
class OrderView:
    order_id: int
    state: OrderState
    created_at: datetime
    updated_at: datetime
    # The earliest date Etsy expects an item of the order to ship; None when unknown.
    ship_by: datetime | None
    overdue: bool
    total: Money
    subtotal: Money
    shipping: Money
    discount: Money
    units: int
    items: tuple[OrderItem, ...]
    shipments: tuple[OrderShipmentView, ...]
    # First name and last initial; only for roles that may see buyer contact details.
    buyer: str | None


@dataclass(frozen=True)
class OrdersPage:
    shop_id: uuid.UUID
    freshness: Freshness
    period: OrderPeriod
    # The period's first moment; for all time, when the shop opened.
    window_start: datetime
    items: tuple[OrderView, ...]
    # Orders matching the filter (before paging).
    total: int
    counts: dict[OrderFilter, int]
    buyer_details: bool
    # True: the period had more orders than `cap`; the newest `cap` are listed.
    partial: bool
    cap: int


@dataclass(frozen=True)
class ListingView:
    listing: ListingDetails
    section_title: str | None


@dataclass(frozen=True)
class ShopListings:
    shop_id: uuid.UUID
    freshness: Freshness
    items: tuple[ListingView, ...]
    total_count: int
    sections: tuple[ShopSection, ...]
    truncated: bool


@dataclass(frozen=True)
class ShopReviews:
    shop_id: uuid.UUID
    freshness: Freshness
    average: Decimal | None
    count: int
    # Stars (1 to 5) to how many of the newest `sample_size` reviews gave them.
    distribution: dict[int, int]
    sample_size: int
    items: tuple[ShopReview, ...]


@dataclass(frozen=True)
class MonthlySales:
    # The first day of the month, in the caller's time zone.
    month: date
    orders: int
    revenue: Decimal


@dataclass(frozen=True)
class CountryOrders:
    # ISO 3166-1 alpha-2 of the orders' destination; country only, never more of the address.
    country_code: str
    orders: int


@dataclass(frozen=True)
class AllTimeSales:
    """Aggregates of the shop's whole order history as read (up to the all-time cap). Sales are
    paid orders that were not cancelled or fully refunded; revenue is their grand total (items,
    shipping and tax, before Etsy's fees) in the shop's currency. No buyer data."""

    currency: str
    orders: int
    units: int
    revenue: Decimal
    # Revenue per sale; None without sales.
    average_order_value: Decimal | None
    # One per month, oldest first, from the first sale's month to this month (none without sales).
    monthly: tuple[MonthlySales, ...]
    best_month: MonthlySales | None
    # Top products by units sold.
    top_products: tuple[TopListing, ...]
    # Sales per destination country, most first; orders without a country are left out.
    countries: tuple[CountryOrders, ...]
    # The oldest order read (any state): where the data starts.
    first_order_at: datetime | None
    receipts_read: int
    # True: the shop had more orders than the cap; the figures cover the newest `receipts_read`.
    partial: bool


@dataclass(frozen=True)
class ShopStats:
    shop_id: uuid.UUID
    freshness: Freshness
    # Opened (`created_at`), total sales, reviews, favourites and listings, from Etsy's counts.
    profile: ShopProfile
    # None when the orders could not be read.
    sales: AllTimeSales | None


@dataclass(frozen=True)
class ShopShipping:
    shop_id: uuid.UUID
    freshness: Freshness
    profiles: tuple[ShippingProfile, ...]
    return_policies: tuple[ReturnPolicy, ...]
    production_partners: tuple[ProductionPartner, ...]


# --- Use cases ---------------------------------------------------------------------------------


async def shop_overview(
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    utc_offset_minutes: int = 0,
) -> ShopOverview:
    """The shop card and the Home screen: the shop's profile and its sales of the last 30 days."""
    _check_offset(utc_offset_minutes)
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_OVERVIEW, now)
    profile = await opened.get("profile", opened.profile)
    sales: Fetched[_ReceiptWindow] | None
    try:
        sales = await opened.get("receipts", opened.receipts)
    except ShopDataUnavailableError, ShopDataScopeMissingError:
        sales = None
    summary = None
    if sales is not None:
        summary = _summarise(sales.value, profile.value.currency_code, now, utc_offset_minutes)
    return ShopOverview(
        shop_id=shop_id,
        freshness=_freshness(profile, *([] if sales is None else [sales])),
        profile=profile.value,
        sales=summary,
    )


async def shop_orders(  # noqa: PLR0913 - the shop, the caller, the period, filter and page
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    period: OrderPeriod = OrderPeriod.DAYS_30,
    state: OrderFilter | None = None,
    limit: int = 50,
    offset: int = 0,
) -> OrdersPage:
    """The Orders screen: the period's orders (30 days by default), newest first, live from the
    marketplace. Completed and shipped orders are included like any other state."""
    if not 1 <= limit <= PAGE or offset < 0:
        raise ValueError("limit must be between 1 and 100 and offset not negative")
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_ORDERS, now)
    contact = _sees_buyer_contact(opened, auth, now)
    window = await opened.get(_RECEIPTS[period], opened.history(period))
    parts: list[Fetched[object]] = [window]
    start = _period_start(period, now)
    if start is None:
        profile = await opened.get("profile", opened.profile)
        parts.append(profile)
        since = profile.value.created_at
        listed = window.value.receipts
    else:
        # The load reads one day more (for the callers' time zones); the list keeps to the period.
        since = start
        listed = tuple(r for r in window.value.receipts if r.created_at >= since)
    orders = [_order_view(r, now, contact=contact) for r in listed]
    counts = Counter[OrderFilter]()
    for order in orders:
        counts[OrderFilter(order.state.value)] += 1
        if order.overdue:
            counts[OrderFilter.OVERDUE] += 1
    matching = [o for o in orders if _matches(o, state)]
    return OrdersPage(
        shop_id=shop_id,
        freshness=_freshness(*parts),
        period=period,
        window_start=since,
        items=tuple(matching[offset : offset + limit]),
        total=len(matching),
        counts={f: counts.get(f, 0) for f in OrderFilter},
        buyer_details=contact,
        partial=window.value.partial,
        cap=RECEIPT_CAPS[period],
    )


async def shop_stats(
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    utc_offset_minutes: int = 0,
) -> ShopStats:
    """The Shops screen's Statistics: Etsy's counts since the shop opened and the aggregates of
    the whole order history (months in the caller's time zone). Aggregates only, no buyer data:
    every role that sees the shop may see them."""
    _check_offset(utc_offset_minutes)
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_OVERVIEW, now)
    profile = await opened.get("profile", opened.profile)
    history: Fetched[_History] | None
    try:
        history = await opened.get_dated(_STATS, opened.history_facts)
    except ShopDataUnavailableError, ShopDataScopeMissingError:
        history = None
    sales = None
    if history is not None:
        sales = _all_time(history.value, profile.value.currency_code, now, utc_offset_minutes)
    return ShopStats(
        shop_id=shop_id,
        freshness=_freshness(profile, *([] if history is None else [history])),
        profile=profile.value,
        sales=sales,
    )


async def shop_listings(
    dashboard: ShopDashboard, auth: AuthContext, shop_id: uuid.UUID, *, now: datetime
) -> ShopListings:
    """The Products screen: the shop's active listings with images, views and favourites."""
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_OVERVIEW, now)
    fetched = await opened.get("listings", opened.listings)
    catalog = fetched.value
    titles = {s.section_id: s.title for s in catalog.sections}
    return ShopListings(
        shop_id=shop_id,
        freshness=_freshness(fetched),
        items=tuple(
            ListingView(listing, titles.get(listing.section_id or 0)) for listing in catalog.items
        ),
        total_count=catalog.total_count,
        sections=catalog.sections,
        truncated=len(catalog.items) < catalog.total_count,
    )


async def shop_reviews(
    dashboard: ShopDashboard, auth: AuthContext, shop_id: uuid.UUID, *, now: datetime
) -> ShopReviews:
    """The newest reviews and how the newest hundred are spread over the stars."""
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_OVERVIEW, now)
    profile = await opened.get("profile", opened.profile)
    fetched = await opened.get("reviews", opened.reviews)
    page = fetched.value
    stars = Counter(review.rating for review in page.items)
    return ShopReviews(
        shop_id=shop_id,
        freshness=_freshness(profile, fetched),
        average=profile.value.review_average,
        count=profile.value.review_count or page.total_count,
        distribution={star: stars.get(star, 0) for star in range(5, 0, -1)},
        sample_size=len(page.items),
        items=page.items[:REVIEW_ITEMS],
    )


async def shop_shipping(
    dashboard: ShopDashboard, auth: AuthContext, shop_id: uuid.UUID, *, now: datetime
) -> ShopShipping:
    """The Shipping screen: shipping profiles, return policies and production partners."""
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_OVERVIEW, now)
    fetched = await opened.get("shipping", opened.shipping)
    profiles, policies, partners = fetched.value
    return ShopShipping(
        shop_id=shop_id,
        freshness=_freshness(fetched),
        profiles=profiles,
        return_policies=policies,
        production_partners=partners,
    )


# --- Opening a shop: authorization, connection, token, cache ------------------------------------


@dataclass(frozen=True)
class _ReceiptWindow:
    # None: the whole history.
    since: datetime | None
    receipts: tuple[OrderReceipt, ...]
    partial: bool


@dataclass(frozen=True)
class _SaleFact:
    """What the statistics need of one receipt: no buyer name, no tracking, country only."""

    created_at: datetime
    is_sale: bool
    total: MarketplaceMoney
    lines: tuple[OrderLine, ...]
    country_code: str | None


@dataclass(frozen=True)
class _History:
    facts: tuple[_SaleFact, ...]
    partial: bool


@dataclass(frozen=True)
class _Catalog:
    items: tuple[ListingDetails, ...]
    total_count: int
    sections: tuple[ShopSection, ...]


_Shipping = tuple[
    tuple[ShippingProfile, ...], tuple[ReturnPolicy, ...], tuple[ProductionPartner, ...]
]


@dataclass(frozen=True)
class _Opened:
    dashboard: ShopDashboard
    integration: ShopIntegration
    tokens: AccessTokenProvider
    now: datetime

    @property
    def etsy(self) -> int:
        return self.integration.etsy_shop_id

    async def get[ValueT](
        self, resource: str, load: Callable[[], Awaitable[ValueT]]
    ) -> Fetched[ValueT]:
        async def undated() -> tuple[ValueT, datetime | None]:
            return await load(), None

        return await self.get_dated(resource, undated)

    async def get_dated[ValueT](
        self, resource: str, load: Callable[[], Awaitable[tuple[ValueT, datetime | None]]]
    ) -> Fetched[ValueT]:
        integration = self.integration
        key = CacheKey(integration.shop_id, integration.generation, resource)
        try:
            fetched = await self.dashboard.cache.get_dated(key, load, POLICIES[resource])
        except MarketplaceAuthError:
            raise ShopNotConnectedError(f"shop {integration.shop_id} is not connected") from None
        except MarketplacePermissionError:
            raise ShopDataScopeMissingError(resource) from None
        except MarketplaceRateLimitedError as error:
            wait = error.retry_after_seconds or 30
            raise ShopDataUnavailableError(max(1, round(wait))) from None
        except MarketplaceError:
            raise ShopDataUnavailableError(30) from None
        if fetched.loaded:
            await self._record_success()
        return fetched

    async def _record_success(self) -> None:
        recorder = self.dashboard.successes
        if recorder is None:
            return
        try:
            # Pinned to the connection the load ran for: a reconnect meanwhile keeps its own
            # time (Codex review of #50).
            await recorder.record_successful_call(
                self.integration.shop_id, at=self.now, generation=self.integration.generation
            )
        except Exception as error:
            _log.warning("shop_data.success_not_recorded: %s", type(error).__name__)

    async def profile(self) -> ShopProfile:
        return await self.dashboard.source.get_shop_profile(self.etsy, tokens=self.tokens)

    async def receipts(self) -> _ReceiptWindow:
        """The last 30 days (the overview's and the default Orders window)."""
        return await self._read_receipts(OrderPeriod.DAYS_30)

    def history(self, period: OrderPeriod) -> Callable[[], Awaitable[_ReceiptWindow]]:
        async def load() -> _ReceiptWindow:
            return await self._read_receipts(period)

        return load

    async def _read_receipts(self, period: OrderPeriod) -> _ReceiptWindow:
        start = _period_start(period, self.now)
        # One day more than the period, so every caller's local window is covered.
        since = None if start is None else start - timedelta(days=1)
        found: list[OrderReceipt] = []
        offset: int | None = 0
        # One page after the other (never in parallel): the adapter paces each call and waits
        # out a 429 before the next.
        for _ in range(RECEIPT_CAPS[period] // PAGE):
            if offset is None:
                break
            page = await self.dashboard.source.list_order_receipts(
                self.etsy, min_created=since, limit=PAGE, offset=offset, tokens=self.tokens
            )
            found.extend(page.items)
            offset = page.next_offset
        newest_first = sorted(found, key=lambda r: (r.created_at, r.receipt_id), reverse=True)
        return _ReceiptWindow(since=since, receipts=tuple(newest_first), partial=offset is not None)

    async def history_facts(self) -> tuple[_History, datetime]:
        """The statistics' basis, from the all-time receipts (shared with the Orders screen's
        All time under their own key), reduced to sale facts without buyer data."""
        resource = _RECEIPTS[OrderPeriod.ALL]
        integration = self.integration
        key = CacheKey(integration.shop_id, integration.generation, resource)
        fetched = await self.dashboard.cache.get(
            key, self.history(OrderPeriod.ALL), POLICIES[resource]
        )
        if fetched.stale:
            # Never stamp old receipts as a fresh basis: the basis' own stale copy stands in.
            raise MarketplaceUnavailableError("the order history is not current")
        if fetched.loaded:
            await self._record_success()
        window = fetched.value
        history = _History(facts=tuple(_fact(r) for r in window.receipts), partial=window.partial)
        # The receipts' fetch time, not now: a basis built from receipts cached minutes ago is
        # that old (Codex review of #51).
        return history, fetched.fetched_at

    async def listings(self) -> _Catalog:
        found: list[ListingDetails] = []
        offset: int | None = 0
        total = 0
        for _ in range(MAX_LISTING_PAGES):
            if offset is None:
                break
            page = await self.dashboard.source.list_listing_details(
                self.etsy, limit=PAGE, offset=offset, tokens=self.tokens
            )
            found.extend(page.items)
            total = page.total_count
            offset = page.next_offset
        sections = await self.dashboard.source.list_sections(self.etsy, tokens=self.tokens)
        ordered = tuple(sorted(sections, key=lambda s: (s.rank, s.section_id)))
        return _Catalog(items=tuple(found), total_count=total, sections=ordered)

    async def reviews(self) -> Page[ShopReview]:
        return await self.dashboard.source.list_reviews(self.etsy, limit=PAGE, tokens=self.tokens)

    async def shipping(self) -> _Shipping:
        source, etsy, tokens = self.dashboard.source, self.etsy, self.tokens
        profiles, policies, partners = await asyncio.gather(
            source.list_shipping_profiles(etsy, tokens=tokens),
            source.list_return_policies(etsy, tokens=tokens),
            source.list_production_partners(etsy, tokens=tokens),
        )
        return profiles, policies, partners


async def _open(
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    capability: Capability,
    now: datetime,
) -> _Opened:
    """Authorize first (a shop the caller may not see does not exist for them), then check the
    connection; no marketplace call is made unless both pass."""
    integration = await dashboard.connections.describe(shop_id)
    if integration is None:
        raise ShopNotFoundError("no such shop")
    try:
        principal = auth.principal_for(integration.organization_id)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop") from None
    shop = ShopRef(id=shop_id, organization_id=integration.organization_id)
    if decide(principal, capability, shop=shop, now=now).outcome is not Outcome.ALLOW:
        raise ShopNotFoundError("no such shop")
    if integration.status is not IntegrationStatus.CONNECTED:
        dashboard.cache.forget(shop_id)
        raise ShopNotConnectedError(f"shop {shop_id} is not connected")
    tokens = dashboard.tokens.pinned(shop_id, generation=integration.generation)
    return _Opened(dashboard, integration, tokens, now)


def _sees_buyer_contact(opened: _Opened, auth: AuthContext, now: datetime) -> bool:
    integration = opened.integration
    principal = auth.principal_for(integration.organization_id)
    shop = ShopRef(id=integration.shop_id, organization_id=integration.organization_id)
    orders = decide(principal, Capability.VIEW_ORDERS, shop=shop, now=now)
    contact = decide(principal, Capability.VIEW_BUYER_CONTACT, shop=shop, now=now)
    return contact.outcome is Outcome.ALLOW and not orders.masked


# --- Shaping -----------------------------------------------------------------------------------


def _check_offset(minutes: int) -> None:
    if not -MAX_UTC_OFFSET_MINUTES <= minutes <= MAX_UTC_OFFSET_MINUTES:
        raise ValueError("the UTC offset must be within 14 hours")


def _freshness(*parts: Fetched[object]) -> Freshness:
    return Freshness(
        source=DataSource.ETSY_LIVE,
        fetched_at=min(part.fetched_at for part in parts),
        stale=any(part.stale for part in parts),
    )


def _money(amount: Decimal, currency: str) -> Money:
    return Money(amount=amount, currency=currency)


def order_state(receipt: OrderReceipt) -> OrderState:
    """How the Orders screen names a receipt's state."""
    if receipt.status == "canceled":
        return OrderState.CANCELED
    if receipt.status == "fully refunded":
        return OrderState.REFUNDED
    if not receipt.is_paid:
        return OrderState.UNPAID
    if receipt.is_shipped or receipt.status == "completed":
        return OrderState.SHIPPED
    return OrderState.PAID


def _ship_by(receipt: OrderReceipt) -> datetime | None:
    dates = [line.expected_ship_date for line in receipt.lines if line.expected_ship_date]
    return min(dates) if dates else None


def _needs_shipping(receipt: OrderReceipt) -> bool:
    return any(not line.is_digital for line in receipt.lines)


def _order_view(receipt: OrderReceipt, now: datetime, *, contact: bool) -> OrderView:
    state = order_state(receipt)
    ship_by = _ship_by(receipt)
    currency = receipt.grand_total.currency_code
    return OrderView(
        order_id=receipt.receipt_id,
        state=state,
        created_at=receipt.created_at,
        updated_at=receipt.updated_at,
        ship_by=ship_by,
        overdue=state is OrderState.PAID
        and _needs_shipping(receipt)
        and ship_by is not None
        and ship_by < now,
        total=_money(receipt.grand_total.as_decimal(), currency),
        subtotal=_money(receipt.subtotal.as_decimal(), receipt.subtotal.currency_code),
        shipping=_money(receipt.shipping_total.as_decimal(), receipt.shipping_total.currency_code),
        discount=_money(receipt.discount_total.as_decimal(), receipt.discount_total.currency_code),
        units=sum(line.quantity for line in receipt.lines),
        items=tuple(
            OrderItem(
                listing_id=line.listing_id,
                title=line.title,
                quantity=line.quantity,
                unit_price=_money(line.unit_price.as_decimal(), line.unit_price.currency_code),
                sku=line.sku,
                is_digital=line.is_digital,
            )
            for line in receipt.lines
        ),
        shipments=tuple(
            OrderShipmentView(
                carrier=shipment.carrier,
                shipped_at=shipment.notified_at,
                tracking_code=(shipment.tracking_code or None) if contact else None,
            )
            for shipment in receipt.shipments
        ),
        buyer=receipt.buyer_display_name if contact else None,
    )


def _matches(order: OrderView, wanted: OrderFilter | None) -> bool:
    if wanted is None:
        return True
    if wanted is OrderFilter.OVERDUE:
        return order.overdue
    return order.state.value == wanted.value


def _is_sale(receipt: OrderReceipt) -> bool:
    return receipt.is_paid and receipt.status not in _NOT_A_SALE


def _summarise(
    window: _ReceiptWindow, currency: str, now: datetime, utc_offset_minutes: int
) -> SalesSummary:
    offset = timedelta(minutes=utc_offset_minutes)
    today = (now.astimezone(UTC) + offset).date()
    first_day = today - timedelta(days=SALES_WINDOW_DAYS - 1)
    days = [first_day + timedelta(days=n) for n in range(SALES_WINDOW_DAYS)]
    orders = dict.fromkeys(days, 0)
    units = dict.fromkeys(days, 0)
    revenue = dict.fromkeys(days, Decimal(0))
    top: dict[int, _Tally] = {}
    awaiting = overdue = 0
    for receipt in window.receipts:
        if not _is_sale(receipt):
            continue
        day = (receipt.created_at.astimezone(UTC) + offset).date()
        if day in orders:
            orders[day] += 1
            units[day] += sum(line.quantity for line in receipt.lines)
            if receipt.grand_total.currency_code == currency:
                revenue[day] += receipt.grand_total.as_decimal()
            _count_listings(top, receipt.lines, currency)
            if order_state(receipt) is OrderState.PAID and _needs_shipping(receipt):
                awaiting += 1
                ship_by = _ship_by(receipt)
                if ship_by is not None and ship_by < now:
                    overdue += 1

    def period(length: int) -> SalesPeriod:
        span = days[-length:]
        return SalesPeriod(
            orders=sum(orders[d] for d in span),
            units=sum(units[d] for d in span),
            revenue=sum((revenue[d] for d in span), Decimal(0)),
        )

    return SalesSummary(
        currency=currency,
        today=period(1),
        last_7_days=period(7),
        last_30_days=period(SALES_WINDOW_DAYS),
        daily=tuple(DailySales(d, orders[d], revenue[d]) for d in days),
        awaiting_shipment=awaiting,
        overdue=overdue,
        top_listings=_top(top),
        partial=window.partial,
    )


@dataclass
class _Tally:
    title: str
    units: int = 0
    revenue: Decimal = Decimal(0)


def _count_listings(top: dict[int, _Tally], lines: tuple[OrderLine, ...], currency: str) -> None:
    for line in lines:
        if line.listing_id is None:
            continue
        tally = top.setdefault(line.listing_id, _Tally(line.title))
        tally.units += line.quantity
        if line.unit_price.currency_code == currency:
            tally.revenue += line.unit_price.as_decimal() * line.quantity


def _top(top: dict[int, _Tally]) -> tuple[TopListing, ...]:
    ranked = sorted(
        (TopListing(listing_id, t.title, t.units, t.revenue) for listing_id, t in top.items()),
        key=lambda t: (-t.units, -t.revenue, t.listing_id or 0),
    )
    return tuple(ranked[:TOP_LISTINGS])


# --- Statistics ----------------------------------------------------------------------------------


def _fact(receipt: OrderReceipt) -> _SaleFact:
    return _SaleFact(
        created_at=receipt.created_at,
        is_sale=_is_sale(receipt),
        total=receipt.grand_total,
        lines=receipt.lines,
        country_code=receipt.country_code,
    )


def _months(first: date, last: date) -> list[date]:
    months = []
    month = first
    while month <= last:
        months.append(month)
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return months


def _average(revenue: Decimal, priced: list[_SaleFact]) -> Decimal | None:
    if not priced:
        return None
    exponent = priced[0].total.as_decimal().as_tuple().exponent
    places = exponent if isinstance(exponent, int) else -2
    return (revenue / len(priced)).quantize(Decimal(1).scaleb(places))


def _all_time(
    history: _History, currency: str, now: datetime, utc_offset_minutes: int
) -> AllTimeSales:
    offset = timedelta(minutes=utc_offset_minutes)

    def month_of(moment: datetime) -> date:
        return (moment.astimezone(UTC) + offset).date().replace(day=1)

    sales = [fact for fact in history.facts if fact.is_sale]
    priced = [fact for fact in sales if fact.total.currency_code == currency]
    revenue = sum((fact.total.as_decimal() for fact in priced), Decimal(0))
    monthly: tuple[MonthlySales, ...] = ()
    if sales:
        months = _months(min(month_of(f.created_at) for f in sales), month_of(now))
        orders = dict.fromkeys(months, 0)
        takings = dict.fromkeys(months, Decimal(0))
        for fact in sales:
            month = month_of(fact.created_at)
            if month in orders:
                orders[month] += 1
                if fact.total.currency_code == currency:
                    takings[month] += fact.total.as_decimal()
        monthly = tuple(MonthlySales(m, orders[m], takings[m]) for m in months)
    top: dict[int, _Tally] = {}
    for fact in sales:
        _count_listings(top, fact.lines, currency)
    countries = Counter(fact.country_code for fact in sales if fact.country_code)
    return AllTimeSales(
        currency=currency,
        orders=len(sales),
        units=sum(line.quantity for fact in sales for line in fact.lines),
        revenue=revenue,
        average_order_value=_average(revenue, priced),
        monthly=monthly,
        best_month=max(
            (m for m in monthly if m.orders), key=lambda m: (m.revenue, m.orders), default=None
        ),
        top_products=_top(top),
        countries=tuple(
            CountryOrders(code, n)
            for code, n in sorted(countries.items(), key=lambda item: (-item[1], item[0]))
        ),
        first_order_at=min((fact.created_at for fact in history.facts), default=None),
        receipts_read=len(history.facts),
        partial=history.partial,
    )


def _period_start(period: OrderPeriod, now: datetime) -> datetime | None:
    """The period's first moment; None for all time. Twelve months are calendar months, from 29
    February to 28 February (Codex review of #51)."""
    if period is OrderPeriod.ALL:
        return None
    if period is OrderPeriod.MONTHS_12:
        try:
            return now.replace(year=now.year - 1)
        except ValueError:  # 29 February
            return now.replace(year=now.year - 1, day=28)
    return now - timedelta(days=_PERIOD_DAYS[period])
