"""Order history beyond 30 days and the shop's statistics since it opened (all values invented).

The owner's report: the shop has reviews, so it had orders, but the Orders screen was empty. The
dashboard read only the last 30 days of receipts, and every order of the shop was older. Orders
now take a period (30 days, 90 days, 12 months, all time), read page by page up to a cap, each
period cached on its own; the statistics aggregate the whole history without buyer data.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from htco_application.marketplace import (
    MarketplacePermissionError,
    MarketplaceUnavailableError,
    OrderReceipt,
)
from htco_application.shop_capabilities import ShopNotConnectedError
from htco_application.shop_dashboard import (
    RECEIPT_CAPS,
    OrderFilter,
    OrderPeriod,
    shop_orders,
    shop_overview,
    shop_stats,
)
from htco_application.shops import ShopNotFoundError
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationStatus
from tests.application.test_shop_dashboard import ISRAEL, OTHER_ORG, SHOP, World, auth
from tests.shop_data_fakes import BUYER, NOW, TRACKING, default_receipts, line, receipt

pytestmark = pytest.mark.anyio

OPENED = datetime(2024, 3, 1, tzinfo=UTC)  # the fake profile's created_at


@pytest.fixture
def world() -> World:
    return World()


def with_history(world: World) -> None:
    """The default month plus completed orders 40 days, 200 days and 500 days ago."""
    world.source.receipts = [
        *default_receipts(),
        receipt(8001, created=NOW - timedelta(days=40), status="completed", shipped=True),
        receipt(8002, created=NOW - timedelta(days=200), status="completed", shipped=True),
        receipt(8003, created=NOW - timedelta(days=500), status="completed", shipped=True),
    ]


# --- The bug: completed orders older than 30 days -----------------------------------------------


async def test_orders__default_period__still_the_last_30_days(world: World) -> None:
    with_history(world)

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW)

    assert page.period is OrderPeriod.DAYS_30
    assert 8001 not in [o.order_id for o in page.items]
    assert page.window_start == NOW - timedelta(days=30)


async def test_orders__all_time__shows_the_completed_orders_older_than_30_days(
    world: World,
) -> None:
    """The owner's shop: every order is old and completed; All time shows them."""
    world.source.receipts = [
        receipt(8001, created=NOW - timedelta(days=40), status="completed", shipped=True),
        receipt(8002, created=NOW - timedelta(days=200), status="completed", shipped=False),
        receipt(8003, created=NOW - timedelta(days=500), status="completed", shipped=True),
    ]

    month = await shop_orders(world.dashboard, auth(), SHOP, now=NOW)
    every = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert month.total == 0
    assert [o.order_id for o in every.items] == [8001, 8002, 8003]
    assert every.total == 3
    assert every.counts[OrderFilter.SHIPPED] == 3
    assert every.period is OrderPeriod.ALL
    # The whole history: no `min_created`; the window starts when the shop opened.
    assert world.source.windows[-1] is None
    assert every.window_start == OPENED


@pytest.mark.parametrize(
    ("period", "days", "shown"),
    [
        (OrderPeriod.DAYS_90, 90, [8001]),
        (OrderPeriod.MONTHS_12, 365, [8001, 8002]),
    ],
)
async def test_orders__a_longer_period__reads_one_day_more_and_lists_its_own_window(
    world: World, period: OrderPeriod, days: int, shown: list[int]
) -> None:
    with_history(world)

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=period)

    assert world.source.windows == [NOW - timedelta(days=days + 1)]
    assert [o.order_id for o in page.items if o.order_id < 9000] == shown
    assert page.window_start == NOW - timedelta(days=days)
    assert page.total == 6 + len(shown)


async def test_orders__no_status_filter_drops_completed_or_shipped_orders(world: World) -> None:
    world.source.receipts = [
        receipt(1, created=NOW - timedelta(days=50), status="completed", shipped=True),
        receipt(2, created=NOW - timedelta(days=51), status="completed", shipped=False),
        receipt(3, created=NOW - timedelta(days=52), status="paid", shipped=True),
        receipt(4, created=NOW - timedelta(days=53), status="canceled"),
        receipt(5, created=NOW - timedelta(days=54), status="fully refunded", shipped=True),
    ]

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.DAYS_90)

    assert page.total == 5
    assert page.counts[OrderFilter.SHIPPED] == 3


@pytest.mark.parametrize(
    ("period", "pages"),
    [
        (OrderPeriod.DAYS_30, 5),
        (OrderPeriod.DAYS_90, 10),
        (OrderPeriod.MONTHS_12, 20),
        (OrderPeriod.ALL, 20),
    ],
)
async def test_orders__more_than_the_cap__reads_pages_in_turn_up_to_it_and_says_partial(
    world: World, period: OrderPeriod, pages: int
) -> None:
    cap = RECEIPT_CAPS[period]
    assert cap == pages * 100
    world.source.receipts = [
        receipt(10_000 + n, created=NOW - timedelta(minutes=n)) for n in range(cap + 1)
    ]

    page = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=period)

    assert world.source.operations().count("list_order_receipts") == pages
    assert page.partial is True
    assert page.cap == cap
    assert page.total == cap


async def test_orders__history_keeps_buyer_details_to_the_roles_that_see_them(
    world: World,
) -> None:
    with_history(world)

    owner = await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    viewer = await shop_orders(
        world.dashboard, auth(Role.VIEWER), SHOP, now=NOW, period=OrderPeriod.ALL
    )

    assert owner.items[-1].buyer == BUYER
    assert {o.buyer for o in viewer.items} == {None}
    assert BUYER not in repr(viewer)
    assert TRACKING not in repr(viewer)


# --- Caching per period ---------------------------------------------------------------------------


async def test_orders__each_period_is_cached_on_its_own(world: World) -> None:
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW)
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert world.source.windows == [NOW - timedelta(days=31), None]


async def test_orders__long_periods_stay_fresh_for_30_minutes(world: World) -> None:
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    world.clock.now = NOW + timedelta(minutes=29)
    await shop_orders(world.dashboard, auth(), SHOP, now=world.clock.now, period=OrderPeriod.ALL)
    assert world.source.operations().count("list_order_receipts") == 1

    world.clock.now = NOW + timedelta(minutes=31)
    await shop_orders(world.dashboard, auth(), SHOP, now=world.clock.now, period=OrderPeriod.ALL)
    assert world.source.operations().count("list_order_receipts") == 2


async def test_orders__history_stale_on_error_with_its_fetch_time(world: World) -> None:
    with_history(world)
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.MONTHS_12)
    world.clock.now = NOW + timedelta(minutes=45)
    world.source.fail["list_order_receipts"] = MarketplaceUnavailableError("down", status=503)

    page = await shop_orders(
        world.dashboard, auth(), SHOP, now=world.clock.now, period=OrderPeriod.MONTHS_12
    )

    assert page.freshness.stale is True
    assert page.freshness.fetched_at == NOW
    assert 8002 in [o.order_id for o in page.items]


async def test_overview__today_and_30_days_unchanged_by_the_history(world: World) -> None:
    with_history(world)

    overview = await shop_overview(
        world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=ISRAEL
    )

    assert overview.sales is not None
    assert overview.sales.today.orders == 2
    assert overview.sales.last_30_days.orders == 4


# --- Statistics since the shop opened -----------------------------------------------------------


def stats_receipts() -> list[OrderReceipt]:
    mug = 701
    towel = 702
    return [
        receipt(1, created=datetime(2026, 10, 14, 12, tzinfo=UTC), country="US"),
        receipt(
            2,
            created=datetime(2026, 9, 10, tzinfo=UTC),
            status="completed",
            shipped=True,
            total_cents=4800,
            lines=(line(21, listing_id=mug, quantity=2),),
            country="IL",
        ),
        # 22:30 UTC on 30 November is 01:30 on 1 December in Israel.
        receipt(
            3,
            created=datetime(2025, 11, 30, 22, 30, tzinfo=UTC),
            status="completed",
            shipped=True,
            total_cents=3000,
            lines=(line(31, listing_id=towel, title="Fake towel", cents=3000),),
            country="US",
        ),
        receipt(
            4,
            created=datetime(2025, 12, 20, tzinfo=UTC),
            status="completed",
            shipped=True,
            total_cents=2000,
            lines=(line(41, listing_id=towel, title="Fake towel", cents=2000),),
            country="DE",
        ),
        receipt(5, created=datetime(2025, 11, 1, tzinfo=UTC), status="canceled", total_cents=9900),
        receipt(6, created=datetime(2025, 11, 2, tzinfo=UTC), status="open", paid=False),
        receipt(7, created=datetime(2025, 11, 3, tzinfo=UTC), status="fully refunded"),
    ]


async def test_stats__profile_figures_since_the_shop_opened(world: World) -> None:
    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    profile = stats.profile
    assert profile.created_at == OPENED
    assert profile.sold_transaction_count == 128
    assert (profile.review_count, profile.review_average) == (31, Decimal("4.8"))
    assert profile.favorer_count == 57
    assert (profile.active_listing_count, profile.digital_listing_count) == (3, 1)
    assert profile.currency_code == "USD"


async def test_stats__totals_average_months_best_month_products_and_countries(
    world: World,
) -> None:
    world.source.receipts = stats_receipts()

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=ISRAEL)

    sales = stats.sales
    assert sales is not None
    assert sales.currency == "USD"
    # Paid sales only: the cancelled, unpaid and refunded orders are not counted.
    assert (sales.orders, sales.units) == (4, 5)
    assert sales.revenue == Decimal("122.00")
    assert sales.average_order_value == Decimal("30.50")
    # One entry per month from the first sale's month to this month, oldest first.
    assert sales.monthly[0].month == date(2025, 12, 1)
    assert sales.monthly[-1].month == date(2026, 10, 1)
    assert len(sales.monthly) == 11
    december = sales.monthly[0]
    assert (december.orders, december.revenue) == (2, Decimal("50.00"))
    assert sales.monthly[1].orders == 0
    assert sales.best_month == december
    mug, towel = sales.top_products
    assert (mug.listing_id, mug.units, mug.revenue) == (701, 3, Decimal("72.00"))
    assert (towel.listing_id, towel.title, towel.units) == (702, "Fake towel", 2)
    assert [(c.country_code, c.orders) for c in sales.countries] == [
        ("US", 2),
        ("DE", 1),
        ("IL", 1),
    ]
    assert sales.first_order_at == datetime(2025, 11, 1, tzinfo=UTC)
    assert (sales.receipts_read, sales.partial) == (7, False)


async def test_stats__months_follow_the_callers_time_zone(world: World) -> None:
    world.source.receipts = stats_receipts()

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=0)

    assert stats.sales is not None
    assert stats.sales.monthly[0].month == date(2025, 11, 1)
    assert stats.sales.monthly[0].orders == 1


async def test_stats__no_sales__zero_totals_and_no_averages(world: World) -> None:
    world.source.receipts = []

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    sales = stats.sales
    assert sales is not None
    assert (sales.orders, sales.revenue, sales.average_order_value) == (0, Decimal(0), None)
    assert sales.best_month is None
    assert sales.monthly == ()
    assert sales.top_products == ()
    assert sales.countries == ()
    assert sales.first_order_at is None


async def test_stats__more_orders_than_the_cap__based_on_the_newest(world: World) -> None:
    cap = RECEIPT_CAPS[OrderPeriod.ALL]
    world.source.receipts = [
        receipt(10_000 + n, created=NOW - timedelta(hours=n)) for n in range(cap + 50)
    ]

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    assert stats.sales is not None
    assert (stats.sales.receipts_read, stats.sales.partial) == (cap, True)
    assert stats.sales.orders == cap


async def test_stats__aggregates_only__no_buyer_or_tracking(world: World) -> None:
    with_history(world)

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    assert BUYER not in repr(stats)
    assert TRACKING not in repr(stats)
    assert not hasattr(stats.sales, "receipts")


async def test_stats__viewer_sees_the_aggregates(world: World) -> None:
    stats = await shop_stats(world.dashboard, auth(Role.VIEWER), SHOP, now=NOW)

    assert stats.sales is not None
    assert stats.sales.orders == 4


async def test_stats__another_organizations_shop__not_found_and_no_call(world: World) -> None:
    with pytest.raises(ShopNotFoundError):
        await shop_stats(world.dashboard, auth(org=OTHER_ORG), SHOP, now=NOW)

    assert world.source.calls == []


async def test_stats__not_connected__no_call(world: World) -> None:
    world.set_status(IntegrationStatus.NEEDS_REAUTH)

    with pytest.raises(ShopNotConnectedError):
        await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    assert world.source.calls == []


async def test_stats__receipts_not_readable__profile_without_sales(world: World) -> None:
    world.source.fail["list_order_receipts"] = MarketplacePermissionError("scope", status=403)

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    assert stats.profile.name == "FakeCraftsShop"
    assert stats.sales is None


@pytest.mark.parametrize("offset", [-841, 841])
async def test_stats__offset_beyond_14_hours__refused(world: World, offset: int) -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        await shop_stats(world.dashboard, auth(), SHOP, now=NOW, utc_offset_minutes=offset)


async def test_stats_and_all_time_orders__share_one_history_load(world: World) -> None:
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    await shop_stats(world.dashboard, auth(), SHOP, now=NOW)

    assert world.source.windows == [None]


async def test_stats__aggregates_stay_fresh_for_6_hours_without_holding_buyer_data(
    world: World,
) -> None:
    """The aggregate basis holds no buyer names, so it may live longer than the receipts (one
    hour): within 6 hours the statistics need no Etsy call, while the all-time order list (with
    names) has left memory and is read again."""
    await shop_stats(world.dashboard, auth(), SHOP, now=NOW)
    world.clock.now = NOW + timedelta(hours=2)

    await shop_stats(world.dashboard, auth(), SHOP, now=world.clock.now)
    assert world.source.windows == [None]

    await shop_orders(world.dashboard, auth(), SHOP, now=world.clock.now, period=OrderPeriod.ALL)
    assert world.source.windows == [None, None]


async def test_stats__etsy_down_after_6_hours__stale_with_the_fetch_time(world: World) -> None:
    await shop_stats(world.dashboard, auth(), SHOP, now=NOW)
    world.clock.now = NOW + timedelta(hours=7)
    world.source.fail["list_order_receipts"] = MarketplaceUnavailableError("down", status=503)

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=world.clock.now)

    assert stats.freshness.stale is True
    assert stats.freshness.fetched_at == NOW
    assert stats.sales is not None
    assert stats.sales.orders == 4


def test_periods__the_four_the_orders_screen_offers() -> None:
    assert [p.value for p in OrderPeriod] == ["30d", "90d", "12m", "all"]


# --- Codex review of #51 ------------------------------------------------------------------------


async def test_stats__built_from_cached_receipts__keep_the_receipts_fetch_time(
    world: World,
) -> None:
    """Codex review of #51: a basis made from receipts cached 29 minutes earlier carries their
    fetch time, so it is not shown as newer than it is and its 6 hours start then."""
    await shop_orders(world.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    world.clock.now = NOW + timedelta(minutes=29)

    stats = await shop_stats(world.dashboard, auth(), SHOP, now=world.clock.now)

    assert stats.freshness.fetched_at == NOW
    assert world.source.windows == [None]
    world.clock.now = NOW + timedelta(hours=6, minutes=5)
    await shop_stats(world.dashboard, auth(), SHOP, now=world.clock.now)
    assert world.source.windows == [None, None]


@pytest.mark.parametrize(
    ("now", "inside", "outside"),
    [
        # Twelve calendar months before 1 March 2024 is 1 March 2023 (365 days would be 2 March).
        (
            datetime(2024, 3, 1, 9, tzinfo=UTC),
            datetime(2023, 3, 1, 10, tzinfo=UTC),
            datetime(2023, 3, 1, 8, tzinfo=UTC),
        ),
        # From a leap day, 28 February of the year before.
        (
            datetime(2024, 2, 29, 9, tzinfo=UTC),
            datetime(2023, 2, 28, 10, tzinfo=UTC),
            datetime(2023, 2, 28, 8, tzinfo=UTC),
        ),
    ],
)
async def test_orders__12_months__twelve_calendar_months(
    world: World, now: datetime, inside: datetime, outside: datetime
) -> None:
    """Codex review of #51: the 12-month period is calendar months, not 365 days."""
    world.clock.now = now
    world.source.receipts = [
        receipt(1, created=inside, status="completed", shipped=True),
        receipt(2, created=outside, status="completed", shipped=True),
    ]

    page = await shop_orders(world.dashboard, auth(), SHOP, now=now, period=OrderPeriod.MONTHS_12)

    assert [o.order_id for o in page.items] == [1]
    assert page.window_start == inside.replace(hour=9)
    assert world.source.windows == [inside.replace(hour=9) - timedelta(days=1)]
