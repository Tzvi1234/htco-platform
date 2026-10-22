"""The connected shop's live data on the dashboard: who sees what, how sales are counted, how
orders are named, and what happens when Etsy does not answer. All values are invented."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.marketplace import (
    MarketplaceAuthError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
)
from htco_application.shop_capabilities import ShopNotConnectedError
from htco_application.shop_dashboard import (
    DataSource,
    OrderFilter,
    OrderState,
    ShopDashboard,
    ShopDataScopeMissingError,
    ShopDataUnavailableError,
    order_state,
    shop_listings,
    shop_orders,
    shop_overview,
    shop_reviews,
    shop_shipping,
)
from htco_application.shop_data_cache import ShopDataCache
from htco_application.shop_integrations import EtsyConnection, OAuthTokens
from htco_application.shops import ShopNotFoundError
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.integrations import IntegrationStatus
from tests.etsy_fakes import ETSY_SHOP, Pinned, Pinning, Vault
from tests.shop_data_fakes import BUYER, NOW, REVIEW_TEXT, TRACKING, ShopData, receipt

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("b0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("a3000000-0000-0000-0000-000000000001")
USER = uuid.UUID("a1000000-0000-0000-0000-000000000001")
ISRAEL = 180


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.vault = Vault()
        self.source = ShopData()
        self.vault.add(
            EtsyConnection(
                organization_id=ORG,
                shop_id=SHOP,
                etsy_shop_id=ETSY_SHOP,
                etsy_user_id=12345678,
                scopes=frozenset({"shops_r", "transactions_r", "listings_r"}),
                tokens=OAuthTokens(
                    access_token="12345678.fake-access-token",  # noqa: S106 - fake
                    refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                    access_expires_at=NOW + timedelta(hours=1),
                    refresh_expires_at=NOW + timedelta(days=90),
                ),
            )
        )
        self.dashboard = ShopDashboard(
            connections=self.vault,
            source=self.source,
            tokens=Pinning(),
            cache=ShopDataCache(clock=self.clock),
            successes=self.vault,
        )

    def set_status(self, status: IntegrationStatus) -> None:
        self.vault.rows[SHOP] = replace(self.vault.rows[SHOP], status=status)


def auth(
    role: Role = Role.OWNER, org: uuid.UUID = ORG, shops: frozenset[uuid.UUID] = frozenset({SHOP})
) -> AuthContext:
    return AuthContext(
        user_id=USER,
        assurance=AssuranceLevel.AAL1,
        session_id=None,
        memberships=(Membership(org, role, shops),),
    )


@pytest.fixture
def world() -> World:
    return World()


# --- Who may look ------------------------------------------------------------------------------


async def test_overview__unknown_shop__not_found(world: World) -> None:
    with pytest.raises(ShopNotFoundError):
        await shop_overview(world.dashboard, auth(), uuid.uuid4(), now=NOW)


async def test_overview__another_organizations_shop__not_found_and_no_call(world: World) -> None:
    with pytest.raises(ShopNotFoundError):
        await shop_overview(world.dashboard, auth(org=OTHER_ORG), SHOP, now=NOW)

    assert world.source.calls == []


async def test_orders__operations_without_a_grant_for_the_shop__not_found(world: World) -> None:
    with pytest.raises(ShopNotFoundError):
        await shop_orders(world.dashboard, auth(Role.OPERATIONS, shops=frozenset()), SHOP, now=NOW)

    assert world.source.calls == []


@pytest.mark.parametrize("status", [IntegrationStatus.NEEDS_REAUTH, IntegrationStatus.DISCONNECTED])
async def test_every_view__connection_not_usable__not_connected_and_no_call(
    world: World, status: IntegrationStatus
) -> None:
    world.set_status(status)

    for view in (shop_overview, shop_orders, shop_listings, shop_reviews, shop_shipping):
        with pytest.raises(ShopNotConnectedError):
            await view(world.dashboard, auth(), SHOP, now=NOW)

    assert world.source.calls == []


async def test_reads__use_a_token_pinned_to_the_connection(world: World) -> None:
    await shop_overview(world.dashboard, auth(), SHOP, now=NOW)

    generation = world.vault.rows[SHOP].generation
    assert {tokens for _, _, tokens in world.source.calls} == {Pinned(SHOP, generation)}
    assert {shop for _, shop, _ in world.source.calls} == {ETSY_SHOP}


# --- Overview and sales ------------------------------------------------------------------------


async def test_overview__profile_and_sales_of_today_week_and_month(world: World) -> None:
    overview = await shop_overview(
        world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=ISRAEL
    )

    assert overview.profile.name == "FakeCraftsShop"
    assert overview.profile.review_average == Decimal("4.8")
    sales = overview.sales
    assert sales is not None
    assert sales.currency == "USD"
    assert (sales.today.orders, sales.today.units, sales.today.revenue) == (2, 3, Decimal("66.00"))
    assert (sales.last_7_days.orders, sales.last_7_days.revenue) == (3, Decimal("90.00"))
    assert (sales.last_30_days.orders, sales.last_30_days.units) == (4, 5)
    assert sales.last_30_days.revenue == Decimal("114.00")
    assert (sales.awaiting_shipment, sales.overdue) == (2, 1)
    assert len(sales.daily) == 30
    assert sales.daily[-1].day.isoformat() == "2026-10-15"
    assert (sales.daily[-1].orders, sales.daily[-1].revenue) == (2, Decimal("66.00"))
    assert sales.partial is False
    [mug, towel] = sales.top_listings
    assert (mug.listing_id, mug.units, mug.revenue) == (701, 4, Decimal("96.00"))
    assert (towel.listing_id, towel.title) == (702, "Fake towel")
    assert overview.freshness.source is DataSource.ETSY_LIVE
    assert overview.freshness.stale is False
    assert overview.freshness.fetched_at == NOW


async def test_overview__today_follows_the_callers_time_zone(world: World) -> None:
    # 00:00 on 15 October at UTC-9: both of "today's" UTC orders were placed yesterday there.
    overview = await shop_overview(world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=-540)

    assert overview.sales is not None
    assert overview.sales.today.orders == 0
    assert overview.sales.daily[-1].day.isoformat() == "2026-10-15"
    assert overview.sales.daily[-2].orders == 2


@pytest.mark.parametrize("offset", [-841, 841])
async def test_overview__offset_beyond_14_hours__refused(world: World, offset: int) -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        await shop_overview(world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=offset)


async def test_overview__receipts_not_readable__profile_without_sales(world: World) -> None:
    world.source.fail["list_order_receipts"] = MarketplacePermissionError("scope", status=403)

    overview = await shop_overview(world.dashboard, auth(), SHOP, now=NOW)

    assert overview.profile.name == "FakeCraftsShop"
    assert overview.sales is None


async def test_overview__viewer_sees_the_same_aggregates(world: World) -> None:
    overview = await shop_overview(world.dashboard, auth(Role.VIEWER), SHOP, now=NOW)

    assert overview.sales is not None
    assert overview.sales.last_30_days.orders == 4


async def test_overview__more_orders_than_one_load_reads__partial(world: World) -> None:
    world.source.receipts = [
        receipt(10_000 + n, created=NOW - timedelta(minutes=n)) for n in range(501)
    ]

    overview = await shop_overview(world.dashboard, auth(), SHOP, now=NOW)

    assert overview.sales is not None
    assert overview.sales.partial is True
    assert world.source.operations().count("list_order_receipts") == 5


# --- Orders ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "paid", "shipped", "state"),
    [
        ("open", False, False, OrderState.UNPAID),
        ("payment processing", False, False, OrderState.UNPAID),
        ("paid", True, False, OrderState.PAID),
        ("partially refunded", True, False, OrderState.PAID),
        ("paid", True, True, OrderState.SHIPPED),
        ("completed", True, False, OrderState.SHIPPED),
        ("canceled", True, False, OrderState.CANCELED),
        ("fully refunded", True, True, OrderState.REFUNDED),
    ],
)
def test_order_state__names_each_receipt_status(
    status: str, paid: bool, shipped: bool, state: OrderState
) -> None:
    found = receipt(1, created=NOW, status=status, paid=paid, shipped=shipped)

    assert order_state(found) is state


async def test_orders__owner__newest_first_with_buyer_tracking_and_counts(world: World) -> None:
    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert [o.order_id for o in page.items] == [9001, 9002, 9003, 9004, 9005, 9006]
    assert page.total == 6
    assert page.buyer_details is True
    assert page.counts == {
        OrderFilter.UNPAID: 1,
        OrderFilter.PAID: 2,
        OrderFilter.SHIPPED: 2,
        OrderFilter.CANCELED: 1,
        OrderFilter.REFUNDED: 0,
        OrderFilter.OVERDUE: 1,
    }
    first, late, shipped = page.items[:3]
    assert first.buyer == BUYER
    assert (first.total.amount, first.total.currency) == (Decimal("48.00"), "USD")
    assert first.units == 2
    assert first.overdue is False
    assert late.overdue is True
    assert late.ship_by == NOW - timedelta(hours=1)
    [sent] = shipped.shipments
    assert (sent.carrier, sent.tracking_code) == ("fake-post", TRACKING)
    assert page.window_start == NOW - timedelta(days=30)
    assert page.freshness.source is DataSource.ETSY_LIVE


@pytest.mark.parametrize("role", [Role.VIEWER, Role.FINANCE, Role.DEVELOPER])
async def test_orders__roles_without_buyer_contact__no_name_no_tracking(
    world: World, role: Role
) -> None:
    page = await shop_orders(world.dashboard, auth(role), SHOP, now=NOW)

    assert page.buyer_details is False
    assert {o.buyer for o in page.items} == {None}
    assert {s.tracking_code for o in page.items for s in o.shipments} == {None}
    assert BUYER not in repr(page)
    assert TRACKING not in repr(page)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.SUPPORT])
async def test_orders__roles_with_buyer_contact__see_the_display_name(
    world: World, role: Role
) -> None:
    page = await shop_orders(world.dashboard, auth(role), SHOP, now=NOW)

    assert page.items[0].buyer == BUYER


async def test_orders__filter_and_page(world: World) -> None:
    overdue = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, state=OrderFilter.OVERDUE)
    shipped = await shop_orders(
        world.dashboard, auth(), SHOP, now=NOW, state=OrderFilter.SHIPPED, limit=1, offset=1
    )

    assert [o.order_id for o in overdue.items] == [9002]
    assert [o.order_id for o in shipped.items] == [9006]
    assert shipped.total == 2


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (10, -1)])
async def test_orders__page_out_of_range__refused(world: World, limit: int, offset: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await shop_orders(world.dashboard, auth(), SHOP, now=NOW, limit=limit, offset=offset)


async def test_orders__digital_only_order__never_overdue(world: World) -> None:
    from tests.shop_data_fakes import line  # noqa: PLC0415 - one test's helper

    world.source.receipts = [
        receipt(
            1,
            created=NOW - timedelta(days=2),
            lines=(line(1, digital=True, ship_by=NOW - timedelta(days=1)),),
        )
    ]

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert page.items[0].overdue is False


# --- Listings, reviews, shipping ---------------------------------------------------------------


async def test_listings__with_section_titles_sections_by_rank(world: World) -> None:
    found = await shop_listings(world.dashboard, auth(Role.VIEWER), SHOP, now=NOW)

    assert [v.listing.listing_id for v in found.items] == [701, 702, 703]
    assert [v.section_title for v in found.items] == ["Fake mugs", "Fake kitchen", None]
    assert [s.title for s in found.sections] == ["Fake mugs", "Fake kitchen"]
    assert (found.total_count, found.truncated) == (3, False)


async def test_reviews__average_count_and_distribution_of_the_newest(world: World) -> None:
    found = await shop_reviews(world.dashboard, auth(), SHOP, now=NOW)

    assert found.average == Decimal("4.8")
    assert found.count == 31
    assert found.distribution == {5: 2, 4: 1, 3: 0, 2: 1, 1: 0}
    assert found.sample_size == 4
    assert found.items[0].text == REVIEW_TEXT
    assert REVIEW_TEXT not in repr(found)


async def test_shipping__profiles_policies_and_partners(world: World) -> None:
    found = await shop_shipping(world.dashboard, auth(), SHOP, now=NOW)

    assert [p.title for p in found.profiles] == ["Fake standard"]
    assert found.return_policies[0].return_deadline_days == 30
    assert found.production_partners[0].name == "Fake Print Partner"


# --- Caching, staleness and failures ------------------------------------------------------------


async def test_overview_then_orders__one_receipts_load_shared(world: World) -> None:
    await shop_overview(world.dashboard, auth(), SHOP, now=NOW)
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert world.source.operations().count("list_order_receipts") == 1


async def test_a_successful_load__moves_the_shop_cards_last_sync(world: World) -> None:
    await shop_listings(world.dashboard, auth(), SHOP, now=NOW)

    assert world.vault.rows[SHOP].last_successful_call_at == NOW


async def test_etsy_down_after_a_load__stale_data_with_its_fetch_time(world: World) -> None:
    await shop_listings(world.dashboard, auth(), SHOP, now=NOW)
    world.clock.now = NOW + timedelta(minutes=20)
    world.source.fail["list_listing_details"] = MarketplaceUnavailableError("down", status=503)

    found = await shop_listings(world.dashboard, auth(), SHOP, now=world.clock.now)

    assert found.freshness.stale is True
    assert found.freshness.fetched_at == NOW
    assert len(found.items) == 3


async def test_etsy_down_and_nothing_cached__unavailable(world: World) -> None:
    world.source.fail["list_reviews"] = MarketplaceUnavailableError("down", status=503)

    with pytest.raises(ShopDataUnavailableError) as raised:
        await shop_reviews(world.dashboard, auth(), SHOP, now=NOW)

    assert raised.value.retry_after_seconds == 30


async def test_rate_limited_and_nothing_cached__unavailable_with_etsys_wait(world: World) -> None:
    world.source.fail["get_shop_profile"] = MarketplaceRateLimitedError(
        "slow", retry_after_seconds=7
    )

    with pytest.raises(ShopDataUnavailableError) as raised:
        await shop_overview(world.dashboard, auth(), SHOP, now=NOW)

    assert raised.value.retry_after_seconds == 7


async def test_scope_missing__scope_error(world: World) -> None:
    world.source.fail["list_shipping_profiles"] = MarketplacePermissionError("scope", status=403)

    with pytest.raises(ShopDataScopeMissingError):
        await shop_shipping(world.dashboard, auth(), SHOP, now=NOW)


async def test_token_refused__not_connected(world: World) -> None:
    world.source.fail["list_order_receipts"] = MarketplaceAuthError("revoked", status=401)

    with pytest.raises(ShopNotConnectedError):
        await shop_orders(world.dashboard, auth(), SHOP, now=NOW)


async def test_disconnect__cached_data_is_forgotten(world: World) -> None:
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW)
    assert len(world.dashboard.cache) == 1

    world.set_status(IntegrationStatus.DISCONNECTED)
    with pytest.raises(ShopNotConnectedError):
        await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert len(world.dashboard.cache) == 0


async def test_reconnect__new_generation_never_sees_the_old_data(world: World) -> None:
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW)
    row = world.vault.rows[SHOP]
    world.vault.rows[SHOP] = replace(row, generation=row.generation + 1)

    await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert world.source.operations().count("list_order_receipts") == 2


def test_now_is_timezone_aware() -> None:
    assert NOW.tzinfo is UTC


# --- Codex review of #50 ------------------------------------------------------------------------


async def test_orders_and_awaiting__keep_to_30_days_though_31_are_read(world: World) -> None:
    world.source.receipts = [
        receipt(1, created=NOW - timedelta(days=2)),
        receipt(2, created=NOW - timedelta(days=30, hours=12)),
    ]

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW)
    overview = await shop_overview(world.dashboard, auth(), SHOP, now=NOW)

    assert [o.order_id for o in page.items] == [1]
    assert page.counts[OrderFilter.PAID] == 1
    assert overview.sales is not None
    assert overview.sales.awaiting_shipment == 1
