"""The connected shop's live data on the dashboard: overview, orders, listings, reviews, shipping.

Read through (Phase 4, "dashboard data"): values come from the marketplace through the read-only
adapter and the short per-shop cache (`shop_data_cache`), with the time they were fetched and
whether they are stale. Orders and statistics come from the stored order history
(`order_history`, `DataSource.HTCO_STORE`) once its backfill is complete; until then they are
read live and the answer says the history is syncing. The results and the API contract are the
same either way.

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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
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
from htco_application.order_history import (
    STALE_AFTER,
    HistorySync,
    OrderStatusChange,
    OrderSync,
    SyncState,
    history_sync,
)
from htco_application.order_ingest import receipt_facts
from htco_application.shop_capabilities import ShopNotConnectedError, TokenPinning
from htco_application.shop_data_cache import CacheKey, CachePolicy, Fetched, ShopDataCache
from htco_application.shop_integrations import ShopIntegration
from htco_application.shops import ShopNotFoundError
from htco_domain.integrations import IntegrationStatus
from htco_domain.orders import OrderState as StoreOrderState
from htco_domain.orders import observed_state

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
    # When the cap was hit: the time of the first receipt past it (no buyer data), cached with
    # its period's receipts.
    "receipts:next": CachePolicy(ttl=timedelta(minutes=2), max_stale=timedelta(hours=1)),
    "receipts:90d:next": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=1)),
    "receipts:12m:next": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=1)),
    # The statistics' basis holds no buyer data (time, totals, items, country), so it is kept
    # longer: fresh for 6 hours, standing in up to Etsy's 24 hours.
    _STATS: CachePolicy(ttl=timedelta(hours=6), max_stale=timedelta(hours=24)),
    "reviews": CachePolicy(ttl=timedelta(minutes=15), max_stale=timedelta(hours=6)),
    "shipping": CachePolicy(ttl=timedelta(minutes=30), max_stale=timedelta(hours=6)),
}


class DataSource(StrEnum):
    """Where a result comes from: Etsy live, or HTCO's stored order history."""

    ETSY_LIVE = "etsy_live"
    HTCO_STORE = "htco_store"


class OrderViewState(StrEnum):
    """The Orders screen's five states: a view of the order store's state machine
    (`htco_domain.orders.OrderState`, FR-ORD-03), mapped by `ORDER_VIEW_STATES`. The API keeps
    Etsy's spelling `canceled`."""

    UNPAID = "unpaid"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELED = "canceled"
    REFUNDED = "refunded"


_S = StoreOrderState
ORDER_VIEW_STATES: Mapping[StoreOrderState, OrderViewState] = {
    _S.UNPAID: OrderViewState.UNPAID,
    _S.PAID: OrderViewState.PAID,
    _S.INGESTED: OrderViewState.PAID,
    _S.AWAITING_SHIPMENT: OrderViewState.PAID,
    _S.SHIPPED: OrderViewState.SHIPPED,
    _S.DELIVERED: OrderViewState.SHIPPED,
    _S.CANCELLED: OrderViewState.CANCELED,
    _S.REFUNDED: OrderViewState.REFUNDED,
}


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


class OrderNotFoundError(Exception):
    """No such order in the shop (stored, or in the live 30-day read)."""

    def __init__(self, order_id: int) -> None:
        super().__init__(f"order {order_id} not found")


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
    # The stored order history; without it orders and statistics are always read live.
    history: OrderSync | None = None


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
    # Where the sales come from: the stored history once complete, Etsy live until then.
    sales_source: DataSource | None = None
    # The stored history's state; None when the store is not set up.
    sync: HistorySync | None = None


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
    state: OrderViewState
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
    # Refunds with Etsy's reason, oldest first, and their sum (None without refunds).
    refunds: tuple[RefundView, ...] = ()
    refunded: Money | None = None


@dataclass(frozen=True)
class RefundView:
    amount: Money
    refunded_at: datetime
    # Etsy's reason code for the refund, when it gives one.
    reason: str | None
    status: str | None
    # The seller's own note on the refund; only for roles that may see buyer contact details.
    note: str | None = field(default=None, repr=False)


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
    # The stored history's state; None when the store is not set up.
    sync: HistorySync | None = None


@dataclass(frozen=True)
class OrderDetail:
    shop_id: uuid.UUID
    freshness: Freshness
    order: OrderView
    # Recorded transitions, oldest first; empty for an order read live.
    timeline: tuple[OrderStatusChange, ...]
    buyer_details: bool
    sync: HistorySync | None = None


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
    # From the stored history only (None when read live): unique buyers among the sales (by the
    # stored name and destination country) and the share who bought more than once, in percent.
    customers: int | None = None
    repeat_customers_percent: Decimal | None = None
    # Refunds and cancellations by Etsy's reason, over every order read.
    refunds_and_cancellations: tuple[ReasonCount, ...] = ()


@dataclass(frozen=True)
class ReasonCount:
    # "refund" or "cancellation".
    kind: str
    # Etsy's reason code; None when it gives none (Etsy's receipt has no cancellation reason of
    # its own: a cancellation takes the reason of its refund).
    reason: str | None
    orders: int
    # Refunded, in the shop's currency.
    amount: Decimal


@dataclass(frozen=True)
class ShopStats:
    shop_id: uuid.UUID
    freshness: Freshness
    # Opened (`created_at`), total sales, reviews, favourites and listings, from Etsy's counts.
    profile: ShopProfile
    # None when the orders could not be read.
    sales: AllTimeSales | None
    # The stored history's state; None when the store is not set up.
    sync: HistorySync | None = None


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
    sync_state = await _sync_state(opened)  # starts the history's sync when due; never waits
    sync = None if dashboard.history is None else history_sync(sync_state, opened.etsy)
    profile = await opened.get("profile", opened.profile)
    currency = profile.value.currency_code
    # One day more than the window, so every caller's local window is covered.
    since = now - timedelta(days=SALES_WINDOW_DAYS + 1)
    stored = await _stored(opened, sync_state, since=since, buyer_data=False)
    if stored is not None and sync_state is not None:
        window = _ReceiptWindow(since=since, receipts=stored, partial=False)
        return ShopOverview(
            shop_id=shop_id,
            freshness=_freshness(profile),
            profile=profile.value,
            sales=_summarise(window, currency, now, utc_offset_minutes),
            sales_source=DataSource.HTCO_STORE,
            sync=sync,
        )
    sales: Fetched[_ReceiptWindow] | None
    try:
        sales = await opened.get("receipts", opened.receipts)
    except ShopDataUnavailableError, ShopDataScopeMissingError:
        sales = None
    summary = None
    if sales is not None:
        summary = _summarise(sales.value, currency, now, utc_offset_minutes)
    return ShopOverview(
        shop_id=shop_id,
        freshness=_freshness(profile, *([] if sales is None else [sales])),
        profile=profile.value,
        sales=summary,
        sales_source=None if sales is None else DataSource.ETSY_LIVE,
        sync=sync,
    )


async def shop_order(
    dashboard: ShopDashboard,
    auth: AuthContext,
    shop_id: uuid.UUID,
    order_id: int,
    *,
    now: datetime,
) -> OrderDetail:
    """One order with its status timeline (slice 5.6): from the stored history, or, for an
    order not stored yet, from the live 30-day read with an empty timeline."""
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_ORDERS, now)
    contact = _sees_buyer_contact(opened, auth, now)
    sync_state = await _sync_state(opened)
    history = dashboard.history
    sync = None if history is None else history_sync(sync_state, opened.etsy)
    if history is not None and sync_state is not None:
        timeline: tuple[OrderStatusChange, ...] = ()
        try:
            found = await history.store.load_order(
                shop_id, opened.etsy, order_id, buyer_data=contact
            )
            timeline = await history.store.timeline(shop_id, order_id) if found else ()
        except Exception as error:
            _log.warning("order_history.read_failed: %s", type(error).__name__)
            found = None
        if found is not None:
            return OrderDetail(
                shop_id=shop_id,
                freshness=_store_freshness(sync_state, now),
                order=_order_view(found, now, contact=contact),
                timeline=tuple(sorted(timeline, key=lambda c: c.observed_at)),
                buyer_details=contact,
                sync=sync,
            )
    window = await opened.get("receipts", opened.receipts)
    live = next((r for r in window.value.receipts if r.receipt_id == order_id), None)
    if live is None:
        raise OrderNotFoundError(order_id)
    return OrderDetail(
        shop_id=shop_id,
        freshness=_freshness(window),
        order=_order_view(live, now, contact=contact),
        timeline=(),
        buyer_details=contact,
        sync=sync,
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
    """The Orders screen: the period's orders (30 days by default), newest first, from the stored
    order history once its backfill is complete, live from the marketplace until then.
    Completed and shipped orders are included like any other state."""
    if not 1 <= limit <= PAGE or offset < 0:
        raise ValueError("limit must be between 1 and 100 and offset not negative")
    opened = await _open(dashboard, auth, shop_id, Capability.VIEW_ORDERS, now)
    contact = _sees_buyer_contact(opened, auth, now)
    sync_state = await _sync_state(opened)
    sync = None if dashboard.history is None else history_sync(sync_state, opened.etsy)
    start = _period_start(period, now)
    stored = await _stored(opened, sync_state, since=start, buyer_data=contact)
    if stored is not None and sync_state is not None:
        since = start if start is not None else await _opened_at(opened, stored)
        listed = stored
        freshness = _store_freshness(sync_state, now)
        partial = False
    else:
        window = await opened.get(_RECEIPTS[period], opened.history(period))
        parts: list[Fetched[object]] = [window]
        if start is None:
            profile = await opened.get("profile", opened.profile)
            parts.append(profile)
            since = profile.value.created_at
            listed = window.value.receipts
        else:
            # The load reads one day more (for the callers' time zones); the list keeps to the
            # period.
            since = start
            listed = tuple(r for r in window.value.receipts if r.created_at >= since)
        freshness = _freshness(*parts)
        partial = await _cut_into(opened, period, window, since, listed)
    orders = [_order_view(r, now, contact=contact) for r in listed]
    counts = Counter[OrderFilter]()
    for order in orders:
        counts[OrderFilter(order.state.value)] += 1
        if order.overdue:
            counts[OrderFilter.OVERDUE] += 1
    matching = [o for o in orders if _matches(o, state)]
    return OrdersPage(
        shop_id=shop_id,
        freshness=freshness,
        period=period,
        window_start=since,
        items=tuple(matching[offset : offset + limit]),
        total=len(matching),
        counts={f: counts.get(f, 0) for f in OrderFilter},
        buyer_details=contact,
        partial=partial,
        cap=RECEIPT_CAPS[period],
        sync=sync,
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
    sync_state = await _sync_state(opened)
    sync = None if dashboard.history is None else history_sync(sync_state, opened.etsy)
    profile = await opened.get("profile", opened.profile)
    currency = profile.value.currency_code
    # Aggregates only: the stored names are read to count unique buyers and never answered.
    stored = await _stored(opened, sync_state, since=None, buyer_data=True)
    if stored is not None and sync_state is not None:
        basis = _History(facts=tuple(_fact(r, with_buyer=True) for r in stored), partial=False)
        return ShopStats(
            shop_id=shop_id,
            freshness=_store_freshness(sync_state, now),
            profile=profile.value,
            sales=_with_customers(_all_time(basis, currency, now, utc_offset_minutes), basis),
            sync=sync,
        )
    history: Fetched[_History] | None
    try:
        history = await opened.get_dated(_STATS, opened.history_facts)
    except ShopDataUnavailableError, ShopDataScopeMissingError:
        history = None
    sales = None
    if history is not None:
        sales = _all_time(history.value, currency, now, utc_offset_minutes)
    return ShopStats(
        shop_id=shop_id,
        freshness=_freshness(profile, *([] if history is None else [history])),
        profile=profile.value,
        sales=sales,
        sync=sync,
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
    # Stored history only: who bought (name and country, normalised), to count unique buyers.
    # Never cached, answered or logged.
    buyer_key: str | None = field(default=None, repr=False)
    status: str = ""
    # Each refund's amount and reason (no note).
    refunds: tuple[tuple[MarketplaceMoney, str | None], ...] = ()


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
        self,
        resource: str,
        load: Callable[[], Awaitable[tuple[ValueT, datetime | None]]],
        *,
        policy: str | None = None,
    ) -> Fetched[ValueT]:
        """`policy`: the name of the cache policy when the resource's name carries more (a
        version)."""
        integration = self.integration
        key = CacheKey(integration.shop_id, integration.generation, resource)
        try:
            fetched = await self.dashboard.cache.get_dated(key, load, POLICIES[policy or resource])
        except MarketplaceAuthError:
            # Refused credentials were reported to the token provider, which marked the
            # connection `needs_reauth`; a refused refresh did the same.
            raise ShopNotConnectedError(
                f"shop {integration.shop_id} is not connected", reason="needs_reauth"
            ) from None
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

    def next_receipt(
        self, since: datetime, offset: int
    ) -> Callable[[], Awaitable[datetime | None]]:
        """The creation time of the receipt at `offset` of the window read from `since` (the
        first one past the cap), or None when there is none: one GET of one receipt."""

        async def load() -> datetime | None:
            page = await self.dashboard.source.list_order_receipts(
                self.etsy, min_created=since, limit=1, offset=offset, tokens=self.tokens
            )
            return page.items[0].created_at if page.items else None

        return load

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
        raise ShopNotConnectedError.of(integration)
    tokens = dashboard.tokens.pinned(shop_id, generation=integration.generation)
    return _Opened(dashboard, integration, tokens, now)


async def _sync_state(opened: _Opened) -> SyncState | None:
    """The stored history's state, starting its sync when due (best effort: a database problem
    never fails a dashboard read; the live read stands in)."""
    history = opened.dashboard.history
    if history is None:
        return None
    try:
        return await history.ensure(opened.integration, now=opened.now)
    except Exception as error:
        _log.warning("order_history.state_unavailable: %s", type(error).__name__)
        return None


async def _stored(
    opened: _Opened, state: SyncState | None, *, since: datetime | None, buyer_data: bool
) -> tuple[OrderReceipt, ...] | None:
    """The stored orders when the backfill is complete; None means read live."""
    history = opened.dashboard.history
    if history is None or state is None or not state.complete_for(opened.etsy):
        return None
    try:
        return await history.store.load(
            opened.integration.shop_id, opened.etsy, since=since, buyer_data=buyer_data
        )
    except Exception as error:
        _log.warning("order_history.read_failed: %s", type(error).__name__)
        return None


async def _opened_at(opened: _Opened, stored: tuple[OrderReceipt, ...]) -> datetime:
    """All time starts when the shop opened (Etsy's profile, cached); without it, at the oldest
    stored order."""
    try:
        profile = await opened.get("profile", opened.profile)
    except ShopDataUnavailableError, ShopDataScopeMissingError:
        return min((r.created_at for r in stored), default=opened.now)
    return profile.value.created_at


def _store_freshness(state: SyncState, now: datetime) -> Freshness:
    synced = state.last_synced_at or now
    return Freshness(
        source=DataSource.HTCO_STORE, fetched_at=synced, stale=now - synced > STALE_AFTER
    )


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


def _machine_state(receipt: OrderReceipt) -> StoreOrderState:
    """The stored `order_state` (slice 5.6); for a receipt read live, what the machine reads
    from its facts."""
    if receipt.stored_state is not None:
        return receipt.stored_state
    return observed_state(receipt_facts(receipt))


def order_state(receipt: OrderReceipt) -> OrderViewState:
    """How the Orders screen names a receipt's state: the store's machine, viewed."""
    return ORDER_VIEW_STATES[_machine_state(receipt)]


def is_sale(receipt: OrderReceipt) -> bool:
    """Cancelled, fully refunded and unpaid orders are not sales (the store's rule)."""
    return _machine_state(receipt).counts_as_sale


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
        overdue=state is OrderViewState.PAID
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
        refunds=tuple(
            RefundView(
                amount=_money(r.amount.as_decimal(), r.amount.currency_code),
                refunded_at=r.created_at,
                reason=r.reason,
                status=r.status,
                note=r.note_from_issuer if contact else None,
            )
            for r in receipt.refunds
        ),
        refunded=_refunded(receipt),
    )


def _refunded(receipt: OrderReceipt) -> Money | None:
    """The refunds' sum in the order's currency (refunds in another currency are left out)."""
    if not receipt.refunds:
        return None
    currency = receipt.grand_total.currency_code
    total = sum(
        (r.amount.as_decimal() for r in receipt.refunds if r.amount.currency_code == currency),
        Decimal(0),
    )
    return _money(total, currency)


def _matches(order: OrderView, wanted: OrderFilter | None) -> bool:
    if wanted is None:
        return True
    if wanted is OrderFilter.OVERDUE:
        return order.overdue
    return order.state.value == wanted.value


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
        if not is_sale(receipt):
            continue
        day = (receipt.created_at.astimezone(UTC) + offset).date()
        if day in orders:
            orders[day] += 1
            units[day] += sum(line.quantity for line in receipt.lines)
            if receipt.grand_total.currency_code == currency:
                revenue[day] += receipt.grand_total.as_decimal()
            _count_listings(top, receipt.lines, currency)
            if order_state(receipt) is OrderViewState.PAID and _needs_shipping(receipt):
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


def _fact(receipt: OrderReceipt, *, with_buyer: bool = False) -> _SaleFact:
    key = None
    if with_buyer and receipt.buyer_name:
        name = " ".join(receipt.buyer_name.casefold().split())
        key = f"{name}|{receipt.country_code or ''}"
    return _SaleFact(
        created_at=receipt.created_at,
        is_sale=is_sale(receipt),
        total=receipt.grand_total,
        lines=receipt.lines,
        country_code=receipt.country_code,
        buyer_key=key,
        status=receipt.status,
        refunds=tuple((r.amount, r.reason) for r in receipt.refunds),
    )


def _reasons(facts: tuple[_SaleFact, ...], currency: str) -> tuple[ReasonCount, ...]:
    """Refunds and cancellations by Etsy's reason: orders and refunded amount (shop currency).
    A cancellation's reason is the reason of its latest refund; None when Etsy gives none."""
    orders: Counter[tuple[str, str | None]] = Counter()
    amounts: dict[tuple[str, str | None], Decimal] = {}
    for fact in facts:
        priced = [(m.as_decimal(), r) for m, r in fact.refunds if m.currency_code == currency]
        per_order: dict[tuple[str, str | None], Decimal] = {}
        if fact.status == "canceled":
            key = ("cancellation", fact.refunds[-1][1] if fact.refunds else None)
            per_order[key] = sum((a for a, _ in priced), Decimal(0))
        else:
            for amount, reason in priced:
                key = ("refund", reason)
                per_order[key] = per_order.get(key, Decimal(0)) + amount
        for key, amount in per_order.items():
            orders[key] += 1
            amounts[key] = amounts.get(key, Decimal(0)) + amount
    ranked = sorted(orders, key=lambda k: (k[0], -orders[k], k[1] or ""))
    return tuple(
        ReasonCount(
            kind=kind, reason=reason, orders=orders[kind, reason], amount=amounts[kind, reason]
        )
        for kind, reason in ranked
    )


def _with_customers(sales: AllTimeSales, history: _History) -> AllTimeSales:
    """Unique buyers among the sales and the share who bought more than once (percent, one
    decimal). Sales without a stored name are not counted."""
    bought = Counter(f.buyer_key for f in history.facts if f.is_sale and f.buyer_key)
    customers = len(bought)
    repeat = sum(1 for n in bought.values() if n > 1)
    percent = None
    if customers:
        percent = (Decimal(repeat) * 100 / customers).quantize(Decimal("0.1"))
    return replace(sales, customers=customers, repeat_customers_percent=percent)


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
        refunds_and_cancellations=_reasons(history.facts, currency),
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


async def _cut_into(
    opened: _Opened,
    period: OrderPeriod,
    fetched: Fetched[_ReceiptWindow],
    since: datetime,
    listed: tuple[OrderReceipt, ...],
) -> bool:
    """Did the cap cut into the period itself (Codex reviews of #51 and #52)? Bounded periods are
    read from one day before them (for time zones): when a receipt read is from that day, the
    period was read whole; when every receipt read is inside the period, the first receipt past
    the cap decides. The look belongs to the window it was made for: its key carries the window's
    fetch time, so a reloaded window never reuses an older look (Codex review of #52)."""
    window = fetched.value
    if not window.partial:
        return False
    if window.since is None:
        return True
    if len(listed) < len(window.receipts):
        return False
    name = f"{_RECEIPTS[period]}:next"
    load = opened.next_receipt(window.since, len(window.receipts))

    async def undated() -> tuple[datetime | None, datetime | None]:
        return await load(), None

    try:
        following = await opened.get_dated(
            f"{name}@{fetched.fetched_at.isoformat()}", undated, policy=name
        )
    except ShopDataUnavailableError:
        return True  # unknown: say the list may be cut rather than claim it is whole
    return following.value is not None and following.value >= since
