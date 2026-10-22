"""The connected shop's live data for the dashboard (Phase 4, "dashboard data").

`GET /v1/shops/{shop_id}/overview`: the shop card and Home: profile, counts, reputation and the
sales of today, the last 7 and 30 days (in the caller's time zone, `utc_offset_minutes`).
`GET /v1/shops/{shop_id}/orders`: the Orders screen, newest first, for a period (`period`: 30
days by default, 90 days, 12 months or all time).
`GET /v1/shops/{shop_id}/listings`: the Products screen, active listings with images.
`GET /v1/shops/{shop_id}/reviews`: the newest reviews and the spread of stars.
`GET /v1/shops/{shop_id}/shipping`: shipping profiles, return policies, production partners.
`GET /v1/shops/{shop_id}/stats`: the Shops screen's Statistics: Etsy's counts since the shop
opened and the aggregates of the whole order history (no buyer data).
`POST /v1/shops/{shop_id}/order-sync`: the Owner or an Admin starts a sync of the stored order
history now (audited; the run goes on in the background).

Every answer says where it comes from (`source`: `etsy_live`, or `htco_store` for orders and
statistics once the stored order history is complete; `sync` says whether it is still syncing),
when it was fetched and whether it is `stale` (Etsy did not answer and older cached data stands
in). Reads only: nothing here writes to Etsy. Buyer
details (a display name, tracking numbers) appear only for roles allowed to see buyer contacts;
order answers are `no-store`. Nothing about an order, a buyer or a review is logged.
"""

import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, WithJsonSchema

from htco_api.auth import CurrentAuth
from htco_api.etsy import not_connected
from htco_api.identity import problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import (
    NOT_FOUND,
    Problem,
    ProblemError,
    ProblemType,
    ShopNotConnectedProblem,
    ValidationProblem,
    request_id_of,
)
from htco_application import shop_dashboard as dashboards
from htco_application.marketplace import (
    ListingState,
    MarketplaceMoney,
    ShopProfile,
    ShopReview,
)
from htco_application.order_history import HistoryState, HistorySync, request_order_sync
from htco_application.shop_capabilities import ShopNotConnectedError
from htco_application.shop_dashboard import (
    DataSource,
    Freshness,
    Money,
    OrderFilter,
    OrderPeriod,
    OrderState,
    ShopDashboard,
    ShopDataScopeMissingError,
    ShopDataUnavailableError,
)
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError

SHOP_DATA_OFF = ProblemType(
    "shop-data-unavailable",
    "Shop data not available",
    503,
    "Live shop data is not set up on this server.",
)
ETSY_DATA_UNAVAILABLE = ProblemType(
    "etsy-data-unavailable",
    "Etsy did not answer",
    503,
    "Etsy did not answer and there is no recent copy to show. Try again shortly.",
)
ETSY_SCOPE_MISSING = ProblemType(
    "etsy-scope-missing",
    "Permission missing at Etsy",
    409,
    "The shop's connection does not allow reading this. Reconnect the shop to grant it.",
)
_SHOP_ERRORS: dict[int, str | tuple[str, type[Problem]]] = {
    404: "No such shop for the caller (or the caller's role may not see it)",
    409: (
        "The shop is not connected (shop-not-connected; `reason` says why, `needs_reauth` "
        "means reconnect it), or the connection lacks the scope (etsy-scope-missing)",
        ShopNotConnectedProblem,
    ),
    422: ("Invalid shop ID or query", ValidationProblem),
    503: "Etsy did not answer and nothing recent is cached (etsy-data-unavailable; see "
    "Retry-After), or live shop data is not set up (shop-data-unavailable)",
}


# --- Views -------------------------------------------------------------------------------------


class MoneyView(BaseModel):
    # A decimal string, exact (for example "25.99").
    amount: Decimal
    currency: str


# The set of sources grows (the stored order history added `htco_store`): declared as an
# extensible enum, so a client handles a value it does not know yet.
_Source = Annotated[
    DataSource,
    WithJsonSchema(
        {
            "type": "string",
            "title": "DataSource",
            "x-extensible-enum": [source.value for source in DataSource],
        }
    ),
]


class FreshnessView(BaseModel):
    source: _Source
    fetched_at: datetime
    stale: bool


class SyncView(BaseModel):
    # `syncing_history`: the backfill is running and the answer is read live from Etsy;
    # `complete`: the answer comes from the stored history (`source: htco_store`).
    state: HistoryState
    orders_stored: int
    last_synced_at: datetime | None


class ShopProfileView(BaseModel):
    etsy_shop_id: int
    name: str
    title: str | None
    url: str | None
    currency: str
    icon_url: str | None
    banner_url: str | None
    is_vacation: bool
    active_listing_count: int
    digital_listing_count: int
    sold_count: int
    favorer_count: int
    review_average: Decimal | None
    review_count: int | None
    opened_at: datetime
    ships_from_country: str | None
    location_country: str | None
    languages: list[str]
    accepts_custom_requests: bool


class SalesPeriodView(BaseModel):
    orders: int
    units: int
    revenue: MoneyView


class DailySalesView(BaseModel):
    date: date
    orders: int
    revenue: Decimal


class TopListingView(BaseModel):
    listing_id: int
    title: str
    units: int
    revenue: MoneyView


class SalesView(BaseModel):
    currency: str
    today: SalesPeriodView
    last_7_days: SalesPeriodView
    last_30_days: SalesPeriodView
    # Oldest first; the last one is today.
    daily: list[DailySalesView]
    awaiting_shipment: int
    overdue: int
    top_listings: list[TopListingView]
    partial: bool


class ShopOverviewView(FreshnessView):
    shop_id: uuid.UUID
    profile: ShopProfileView
    # Null when the orders could not be read (the profile still shows).
    sales: SalesView | None


class OrderItemView(BaseModel):
    listing_id: int | None
    title: str
    quantity: int
    unit_price: MoneyView
    sku: str | None
    is_digital: bool


class ShipmentView(BaseModel):
    carrier: str
    shipped_at: datetime
    tracking_code: str | None


class RefundView(BaseModel):
    amount: MoneyView
    refunded_at: datetime
    # Etsy's reason for the refund, when it gives one.
    reason: str | None
    status: str | None
    # The seller's note on the refund, only for roles that see buyer contacts.
    note: str | None


class OrderView(BaseModel):
    order_id: int
    state: OrderState
    created_at: datetime
    updated_at: datetime
    ship_by: datetime | None
    overdue: bool
    total: MoneyView
    subtotal: MoneyView
    shipping: MoneyView
    discount: MoneyView
    units: int
    items: list[OrderItemView]
    shipments: list[ShipmentView]
    # First name and last initial, only for roles that see buyer contacts.
    buyer: str | None
    # Oldest first; empty without refunds.
    refunds: list[RefundView] = []
    # The refunds' sum in the order's currency; null without refunds.
    refunded_total: MoneyView | None = None


class OrderCountsView(BaseModel):
    unpaid: int
    paid: int
    shipped: int
    canceled: int
    refunded: int
    overdue: int


class OrdersView(FreshnessView):
    shop_id: uuid.UUID
    period: OrderPeriod
    # The period's first moment; for all time, when the shop opened.
    window_start: datetime
    items: list[OrderView]
    total: int
    counts: OrderCountsView
    # True when buyer names and tracking numbers are included for this caller.
    buyer_details: bool
    # True when the period had more orders than `cap`: the newest `cap` are listed.
    partial: bool
    cap: int
    # The stored order history; null when it is not set up on this server.
    sync: SyncView | None = None


class ListingView(BaseModel):
    listing_id: int
    title: str
    state: ListingState
    quantity: int
    price: MoneyView
    url: str | None
    image_url: str | None
    thumbnail_url: str | None
    image_alt: str | None
    views: int
    favorer_count: int
    tags: list[str]
    section_id: int | None
    section_title: str | None
    is_digital: bool
    is_personalizable: bool
    has_variations: bool
    created_at: datetime
    updated_at: datetime


class SectionView(BaseModel):
    section_id: int
    title: str
    active_listing_count: int


class ListingsView(FreshnessView):
    shop_id: uuid.UUID
    items: list[ListingView]
    total_count: int
    sections: list[SectionView]
    truncated: bool


class ReviewView(BaseModel):
    listing_id: int
    rating: int
    text: str | None
    language: str | None
    created_at: datetime


class StarCountView(BaseModel):
    stars: int
    count: int


class ReviewsView(FreshnessView):
    shop_id: uuid.UUID
    average: Decimal | None
    count: int
    # Five to one star, over the newest `sample_size` reviews.
    distribution: list[StarCountView]
    sample_size: int
    items: list[ReviewView]


class DestinationView(BaseModel):
    country: str | None
    region: str | None
    first_item: MoneyView
    additional_item: MoneyView
    min_delivery_days: int | None
    max_delivery_days: int | None
    mail_class: str | None


class ShippingProfileView(BaseModel):
    profile_id: int
    title: str | None
    origin_country: str
    profile_type: str
    destinations: list[DestinationView]
    upgrade_count: int


class ReturnPolicyView(BaseModel):
    policy_id: int
    accepts_returns: bool
    accepts_exchanges: bool
    return_deadline_days: int | None


class ProductionPartnerView(BaseModel):
    partner_id: int
    name: str
    location: str


class ShippingView(FreshnessView):
    shop_id: uuid.UUID
    profiles: list[ShippingProfileView]
    return_policies: list[ReturnPolicyView]
    production_partners: list[ProductionPartnerView]


class MonthlySalesView(BaseModel):
    # The first day of the month in the caller's time zone.
    month: date
    orders: int
    revenue: Decimal


class CountryOrdersView(BaseModel):
    # ISO 3166-1 alpha-2 of the orders' destination (country only).
    country: str
    orders: int


class ReasonCountView(BaseModel):
    # `refund` or `cancellation`.
    kind: str
    # Etsy's reason; null when it gives none.
    reason: str | None
    orders: int
    # Refunded, in the shop's currency.
    amount: MoneyView


class AllTimeSalesView(BaseModel):
    currency: str
    # Paid orders that were not cancelled or fully refunded.
    orders: int
    units: int
    # Gross: the orders' grand total (items, shipping, tax) before Etsy's fees.
    revenue: MoneyView
    average_order_value: MoneyView | None
    # Oldest first, from the first sale's month to this month.
    monthly: list[MonthlySalesView]
    best_month: MonthlySalesView | None
    top_products: list[TopListingView]
    countries: list[CountryOrdersView]
    # The oldest order read: where the figures start.
    first_order_at: datetime | None
    receipts_read: int
    # True when the shop had more orders than one load reads: the figures cover the newest
    # `receipts_read`.
    partial: bool
    # From the stored history only (null when read live): unique buyers among the sales and the
    # share of them who bought more than once, in percent.
    customers: int | None = None
    repeat_customers_percent: Decimal | None = None
    # Refunds and cancellations by Etsy's reason.
    refunds_and_cancellations: list[ReasonCountView] = []


class ShopStatsView(FreshnessView):
    shop_id: uuid.UUID
    profile: ShopProfileView
    # Null when the orders could not be read (the profile still shows).
    sales: AllTimeSalesView | None
    # The stored order history; null when it is not set up on this server.
    sync: SyncView | None = None


# --- Mapping -----------------------------------------------------------------------------------


def _fresh(freshness: Freshness) -> dict[str, Any]:
    return {
        "source": freshness.source,
        "fetched_at": freshness.fetched_at,
        "stale": freshness.stale,
    }


def _money(money: Money) -> MoneyView:
    return MoneyView(amount=money.amount, currency=money.currency)


def _sync(sync: HistorySync | None) -> SyncView | None:
    if sync is None:
        return None
    return SyncView(
        state=sync.state, orders_stored=sync.orders_stored, last_synced_at=sync.last_synced_at
    )


def _marketplace_money(money: MarketplaceMoney) -> MoneyView:
    return MoneyView(amount=money.as_decimal(), currency=money.currency_code)


def _overview(found: dashboards.ShopOverview) -> ShopOverviewView:
    sales = found.sales
    return ShopOverviewView(
        **_fresh(found.freshness),
        shop_id=found.shop_id,
        profile=_profile(found.profile),
        sales=None if sales is None else _sales(sales),
    )


def _profile(p: ShopProfile) -> ShopProfileView:
    return ShopProfileView(
        etsy_shop_id=p.shop_id,
        name=p.name,
        title=p.title,
        url=p.url,
        currency=p.currency_code,
        icon_url=p.icon_url,
        banner_url=p.banner_url,
        is_vacation=p.is_vacation,
        active_listing_count=p.active_listing_count,
        digital_listing_count=p.digital_listing_count,
        sold_count=p.sold_transaction_count,
        favorer_count=p.favorer_count,
        review_average=p.review_average,
        review_count=p.review_count,
        opened_at=p.created_at,
        ships_from_country=p.ships_from_country,
        location_country=p.location_country,
        languages=list(p.languages),
        accepts_custom_requests=p.accepts_custom_requests,
    )


def _month(month: dashboards.MonthlySales) -> MonthlySalesView:
    return MonthlySalesView(month=month.month, orders=month.orders, revenue=month.revenue)


def _all_time(sales: dashboards.AllTimeSales) -> AllTimeSalesView:
    currency = sales.currency
    average = sales.average_order_value
    return AllTimeSalesView(
        currency=currency,
        orders=sales.orders,
        units=sales.units,
        revenue=MoneyView(amount=sales.revenue, currency=currency),
        average_order_value=None
        if average is None
        else MoneyView(amount=average, currency=currency),
        monthly=[_month(m) for m in sales.monthly],
        best_month=None if sales.best_month is None else _month(sales.best_month),
        top_products=[
            TopListingView(
                listing_id=t.listing_id,
                title=t.title,
                units=t.units,
                revenue=MoneyView(amount=t.revenue, currency=currency),
            )
            for t in sales.top_products
        ],
        countries=[
            CountryOrdersView(country=c.country_code, orders=c.orders) for c in sales.countries
        ],
        first_order_at=sales.first_order_at,
        receipts_read=sales.receipts_read,
        partial=sales.partial,
        customers=sales.customers,
        repeat_customers_percent=sales.repeat_customers_percent,
        refunds_and_cancellations=[
            ReasonCountView(
                kind=r.kind,
                reason=r.reason,
                orders=r.orders,
                amount=MoneyView(amount=r.amount, currency=currency),
            )
            for r in sales.refunds_and_cancellations
        ],
    )


def _stats(found: dashboards.ShopStats) -> ShopStatsView:
    return ShopStatsView(
        **_fresh(found.freshness),
        shop_id=found.shop_id,
        profile=_profile(found.profile),
        sales=None if found.sales is None else _all_time(found.sales),
        sync=_sync(found.sync),
    )


def _period(period: dashboards.SalesPeriod, currency: str) -> SalesPeriodView:
    return SalesPeriodView(
        orders=period.orders,
        units=period.units,
        revenue=MoneyView(amount=period.revenue, currency=currency),
    )


def _sales(sales: dashboards.SalesSummary) -> SalesView:
    currency = sales.currency
    return SalesView(
        currency=currency,
        today=_period(sales.today, currency),
        last_7_days=_period(sales.last_7_days, currency),
        last_30_days=_period(sales.last_30_days, currency),
        daily=[DailySalesView(date=d.day, orders=d.orders, revenue=d.revenue) for d in sales.daily],
        awaiting_shipment=sales.awaiting_shipment,
        overdue=sales.overdue,
        top_listings=[
            TopListingView(
                listing_id=t.listing_id,
                title=t.title,
                units=t.units,
                revenue=MoneyView(amount=t.revenue, currency=currency),
            )
            for t in sales.top_listings
        ],
        partial=sales.partial,
    )


def _order(order: dashboards.OrderView) -> OrderView:
    return OrderView(
        order_id=order.order_id,
        state=order.state,
        created_at=order.created_at,
        updated_at=order.updated_at,
        ship_by=order.ship_by,
        overdue=order.overdue,
        total=_money(order.total),
        subtotal=_money(order.subtotal),
        shipping=_money(order.shipping),
        discount=_money(order.discount),
        units=order.units,
        items=[
            OrderItemView(
                listing_id=item.listing_id,
                title=item.title,
                quantity=item.quantity,
                unit_price=_money(item.unit_price),
                sku=item.sku,
                is_digital=item.is_digital,
            )
            for item in order.items
        ],
        shipments=[
            ShipmentView(carrier=s.carrier, shipped_at=s.shipped_at, tracking_code=s.tracking_code)
            for s in order.shipments
        ],
        buyer=order.buyer,
        refunds=[
            RefundView(
                amount=_money(r.amount),
                refunded_at=r.refunded_at,
                reason=r.reason,
                status=r.status,
                note=r.note,
            )
            for r in order.refunds
        ],
        refunded_total=None if order.refunded is None else _money(order.refunded),
    )


def _orders(page: dashboards.OrdersPage) -> OrdersView:
    counts = {f.value: n for f, n in page.counts.items()}
    return OrdersView(
        **_fresh(page.freshness),
        shop_id=page.shop_id,
        period=page.period,
        window_start=page.window_start,
        items=[_order(order) for order in page.items],
        total=page.total,
        counts=OrderCountsView(**counts),
        buyer_details=page.buyer_details,
        partial=page.partial,
        cap=page.cap,
        sync=_sync(page.sync),
    )


def _listings(found: dashboards.ShopListings) -> ListingsView:
    return ListingsView(
        **_fresh(found.freshness),
        shop_id=found.shop_id,
        items=[
            ListingView(
                listing_id=v.listing.listing_id,
                title=v.listing.title,
                state=v.listing.state,
                quantity=v.listing.quantity,
                price=_marketplace_money(v.listing.price),
                url=v.listing.url,
                image_url=None if v.listing.image is None else v.listing.image.url,
                thumbnail_url=None if v.listing.image is None else v.listing.image.thumbnail_url,
                image_alt=None if v.listing.image is None else v.listing.image.alt_text,
                views=v.listing.views,
                favorer_count=v.listing.favorer_count,
                tags=list(v.listing.tags),
                section_id=v.listing.section_id,
                section_title=v.section_title,
                is_digital=v.listing.is_digital,
                is_personalizable=v.listing.is_personalizable,
                has_variations=v.listing.has_variations,
                created_at=v.listing.created_at,
                updated_at=v.listing.updated_at,
            )
            for v in found.items
        ],
        total_count=found.total_count,
        sections=[
            SectionView(
                section_id=s.section_id, title=s.title, active_listing_count=s.active_listing_count
            )
            for s in found.sections
        ],
        truncated=found.truncated,
    )


def _review(review: ShopReview) -> ReviewView:
    return ReviewView(
        listing_id=review.listing_id,
        rating=review.rating,
        text=review.text,
        language=review.language,
        created_at=review.created_at,
    )


def _reviews(found: dashboards.ShopReviews) -> ReviewsView:
    return ReviewsView(
        **_fresh(found.freshness),
        shop_id=found.shop_id,
        average=found.average,
        count=found.count,
        distribution=[StarCountView(stars=s, count=n) for s, n in found.distribution.items()],
        sample_size=found.sample_size,
        items=[_review(r) for r in found.items],
    )


def _shipping(found: dashboards.ShopShipping) -> ShippingView:
    return ShippingView(
        **_fresh(found.freshness),
        shop_id=found.shop_id,
        profiles=[
            ShippingProfileView(
                profile_id=p.profile_id,
                title=p.title,
                origin_country=p.origin_country,
                profile_type=p.profile_type,
                destinations=[
                    DestinationView(
                        country=d.country_code,
                        region=d.region,
                        first_item=_marketplace_money(d.first_item_cost),
                        additional_item=_marketplace_money(d.additional_item_cost),
                        min_delivery_days=d.min_delivery_days,
                        max_delivery_days=d.max_delivery_days,
                        mail_class=d.mail_class,
                    )
                    for d in p.destinations
                ],
                upgrade_count=p.upgrade_count,
            )
            for p in found.profiles
        ],
        return_policies=[
            ReturnPolicyView(
                policy_id=r.policy_id,
                accepts_returns=r.accepts_returns,
                accepts_exchanges=r.accepts_exchanges,
                return_deadline_days=r.return_deadline_days,
            )
            for r in found.return_policies
        ],
        production_partners=[
            ProductionPartnerView(partner_id=p.partner_id, name=p.name, location=p.location)
            for p in found.production_partners
        ],
    )


# --- Routes ------------------------------------------------------------------------------------


def _dashboard(request: Request) -> ShopDashboard:
    found = getattr(request.app.state, "dashboard", None)
    if not isinstance(found, ShopDashboard):
        raise ProblemError(SHOP_DATA_OFF)
    return found


_Dashboard = Annotated[ShopDashboard, Depends(_dashboard)]


def _problem_of(error: Exception) -> ProblemError:
    match error:
        case ShopNotFoundError():
            return ProblemError(NOT_FOUND)
        case ShopNotConnectedError():
            return not_connected(error)
        case ShopDataScopeMissingError():
            return ProblemError(ETSY_SCOPE_MISSING)
        case ShopDataUnavailableError(retry_after_seconds=wait):
            return ProblemError(ETSY_DATA_UNAVAILABLE, headers={"Retry-After": str(wait)})
    raise error  # pragma: no cover - only the errors above are passed in


_KNOWN = (
    ShopNotFoundError,
    ShopNotConnectedError,
    ShopDataScopeMissingError,
    ShopDataUnavailableError,
)
_PRIVATE = "private, no-cache"


def shop_data_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1/shops/{shop_id}", tags=["shop data"])

    @router.get(
        "/overview",
        summary="The shop's profile and sales, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def get_shop_overview(
        shop_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        dashboard: _Dashboard,
        utc_offset_minutes: Annotated[int, Query(ge=-840, le=840)] = 0,
    ) -> ShopOverviewView:
        """Every role that sees the shop. Sales count paid orders that were not cancelled or
        fully refunded, by the day they were placed at `utc_offset_minutes` from UTC."""
        try:
            found = await dashboards.shop_overview(
                dashboard, auth, shop_id, now=clock(), utc_offset_minutes=utc_offset_minutes
            )
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = _PRIVATE
        return _overview(found)

    @router.get(
        "/orders",
        summary="The shop's orders of a period, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def list_shop_orders(  # noqa: PLR0913, PLR0917 - FastAPI passes one parameter per query filter
        shop_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        dashboard: _Dashboard,
        period: OrderPeriod = OrderPeriod.DAYS_30,
        state: OrderFilter | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> OrdersView:
        """Newest first, every state (completed and shipped included). `period`: `30d` (the
        default), `90d`, `12m` or `all`; up to `cap` orders a period (500, 1,000, 2,000, 2,000).
        Buyer display names and tracking numbers only for Owner, Admin, Operations and Support;
        never cached by the browser."""
        try:
            page = await dashboards.shop_orders(
                dashboard,
                auth,
                shop_id,
                now=clock(),
                period=period,
                state=state,
                limit=limit,
                offset=offset,
            )
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = "no-store"
        return _orders(page)

    @router.get(
        "/listings",
        summary="The shop's active listings, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def list_shop_listings(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, dashboard: _Dashboard
    ) -> ListingsView:
        """Images, price, stock, views, favourites, tags and sections; up to 300 listings."""
        try:
            found = await dashboards.shop_listings(dashboard, auth, shop_id, now=clock())
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = _PRIVATE
        return _listings(found)

    @router.get(
        "/reviews",
        summary="The shop's newest reviews, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def list_shop_reviews(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, dashboard: _Dashboard
    ) -> ReviewsView:
        """The newest 25 reviews, the shop's average and count, and the stars of the newest 100."""
        try:
            found = await dashboards.shop_reviews(dashboard, auth, shop_id, now=clock())
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = _PRIVATE
        return _reviews(found)

    @router.get(
        "/shipping",
        summary="The shop's shipping profiles and policies, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def get_shop_shipping(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, dashboard: _Dashboard
    ) -> ShippingView:
        """Shipping profiles with destinations and costs, return policies, production partners."""
        try:
            found = await dashboards.shop_shipping(dashboard, auth, shop_id, now=clock())
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = _PRIVATE
        return _shipping(found)

    @router.get(
        "/stats",
        summary="The shop's statistics since it opened, live from the marketplace",
        responses=problem_responses(_SHOP_ERRORS),
    )
    async def get_shop_stats(
        shop_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        dashboard: _Dashboard,
        utc_offset_minutes: Annotated[int, Query(ge=-840, le=840)] = 0,
    ) -> ShopStatsView:
        """Every role that sees the shop: Etsy's counts (opened, total sales, reviews,
        favourites, listings) and the aggregates of the order history read (up to 2,000 orders;
        `partial` says the cap was hit), with months at `utc_offset_minutes` from UTC. No buyer
        data."""
        try:
            found = await dashboards.shop_stats(
                dashboard, auth, shop_id, now=clock(), utc_offset_minutes=utc_offset_minutes
            )
        except _KNOWN as error:
            raise _problem_of(error) from None
        response.headers["Cache-Control"] = _PRIVATE
        return _stats(found)

    _add_sync_route(router, clock=clock)
    return router


def _add_sync_route(router: APIRouter, *, clock: Callable[[], datetime]) -> None:
    """The manual sync of the stored order history."""

    @router.post(
        "/order-sync",
        status_code=202,
        summary="Start a sync of the shop's stored order history now",
        responses=problem_responses(
            {
                403: "Only the Owner and Admins start a sync (forbidden), or the request did not "
                "come from the web app (csrf-failed)",
                404: "No such shop for the caller",
                409: (
                    "The shop is not connected (shop-not-connected; `reason` says why)",
                    ShopNotConnectedProblem,
                ),
                422: ("Invalid shop ID", ValidationProblem),
                503: "The stored order history is not set up on this server "
                "(shop-data-unavailable)",
            }
        ),
    )
    async def start_order_sync(
        shop_id: uuid.UUID, request: Request, auth: CurrentAuth, dashboard: _Dashboard
    ) -> SyncView:
        """The backfill from its checkpoint (or from the start), then the last 30 days; Etsy is
        only read. Audited with the trigger only. Answers at once; the run goes on in the
        background and one runs per shop at a time."""
        history = dashboard.history
        if history is None:
            raise ProblemError(SHOP_DATA_OFF)
        try:
            sync = await request_order_sync(
                history,
                dashboard.connections,
                auth,
                shop_id,
                now=clock(),
                correlation_id=request_id_of(request),
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        except ShopNotConnectedError as error:
            raise not_connected(error) from None
        return SyncView(
            state=sync.state, orders_stored=sync.orders_stored, last_synced_at=sync.last_synced_at
        )
