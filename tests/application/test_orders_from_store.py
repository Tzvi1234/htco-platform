"""Slice 5.6: the existing Orders, Home and Statistics answers read the stored order once the
backfill is complete, the screen's state is the stored `order_state`, and one order comes with
its status timeline from `order_status_events` (all values invented)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from htco_application.order_history import OrderStatusChange
from htco_application.order_ingest import IngestSource
from htco_application.shop_dashboard import (
    DataSource,
    OrderNotFoundError,
    OrderViewState,
    shop_order,
    shop_orders,
    shop_overview,
    shop_stats,
)
from htco_application.shops import ShopNotFoundError
from htco_domain.identity import Role
from htco_domain.orders import OrderState
from tests.application.test_order_history import Setup, many
from tests.application.test_shop_dashboard import OTHER_ORG, SHOP, auth
from tests.shop_data_fakes import NOW, receipt

pytestmark = pytest.mark.anyio


async def stored(count: int = 3) -> Setup:
    setup = Setup(many(count))
    await setup.sync.run(setup.integration)
    setup.recording.offsets.clear()
    return setup


# --- the stored state is the screen's state -----------------------------------------------------


@pytest.mark.ac("AC-5.6.4")
async def test_orders__the_stored_order_state_wins_over_the_receipts_flags() -> None:
    setup = await stored()
    key = (SHOP, 100_000)
    # Etsy's copy says paid and not shipped; the store's machine already recorded the delivery.
    setup.store.orders[key] = replace(
        setup.store.orders[key], status="paid", is_shipped=False, stored_state=OrderState.DELIVERED
    )

    page = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)

    [order] = [o for o in page.items if o.order_id == 100_000]
    assert order.state is OrderViewState.SHIPPED
    assert order.overdue is False


@pytest.mark.ac("AC-5.6.4")
async def test_orders__a_stored_refund__never_counted_as_a_sale_on_home() -> None:
    setup = await stored()
    key = (SHOP, 100_000)
    setup.store.orders[key] = replace(
        setup.store.orders[key], status="completed", stored_state=OrderState.REFUNDED
    )

    overview = await shop_overview(setup.dashboard, auth(), SHOP, now=NOW)

    assert overview.sales is not None
    assert overview.sales.last_30_days.orders == 2


# --- Home reads the store ------------------------------------------------------------------------


@pytest.mark.ac("AC-5.6.4")
async def test_overview__complete_store__sales_from_the_store_with_no_receipt_call() -> None:
    setup = await stored()

    overview = await shop_overview(setup.dashboard, auth(), SHOP, now=NOW)

    assert overview.sales is not None
    assert overview.sales.last_30_days.orders == 3
    assert setup.recording.offsets == []  # no receipts read from Etsy
    assert overview.sales_source is DataSource.HTCO_STORE


async def test_overview__while_the_backfill_runs__live_sales_as_the_fallback() -> None:
    setup = Setup(many(3))

    overview = await shop_overview(setup.dashboard, auth(), SHOP, now=NOW)

    assert overview.sales is not None
    assert overview.sales_source is DataSource.ETSY_LIVE


@pytest.mark.ac("AC-5.6.3")
async def test_every_answer__says_when_the_history_last_synced() -> None:
    setup = await stored()

    orders = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)
    overview = await shop_overview(setup.dashboard, auth(), SHOP, now=NOW)
    stats = await shop_stats(setup.dashboard, auth(), SHOP, now=NOW)
    one = await shop_order(setup.dashboard, auth(), SHOP, 100_000, now=NOW)

    for answer in (orders, overview, stats, one):
        assert answer.sync is not None
        assert answer.sync.last_synced_at is not None


# --- one order with its timeline -----------------------------------------------------------------


@pytest.mark.ac("AC-5.6.5")
async def test_order__from_the_store_with_its_timeline_in_causal_order() -> None:
    setup = await stored()
    paid_at = NOW - timedelta(days=1, hours=2)
    setup.store.timelines[(SHOP, 100_000)] = [
        OrderStatusChange(OrderState.SHIPPED, OrderState.DELIVERED, IngestSource.WEBHOOK, NOW),
        OrderStatusChange(None, OrderState.PAID, IngestSource.BACKFILL, paid_at),
        OrderStatusChange(OrderState.PAID, OrderState.INGESTED, IngestSource.BACKFILL, paid_at),
    ]

    one = await shop_order(setup.dashboard, auth(), SHOP, 100_000, now=NOW)

    assert one.order.order_id == 100_000
    assert one.freshness.source is DataSource.HTCO_STORE
    assert [(c.from_state, c.to_state, c.source) for c in one.timeline] == [
        (None, OrderState.PAID, IngestSource.BACKFILL),
        (OrderState.PAID, OrderState.INGESTED, IngestSource.BACKFILL),
        (OrderState.SHIPPED, OrderState.DELIVERED, IngestSource.WEBHOOK),
    ]
    assert setup.recording.offsets == []


async def test_order__not_stored_yet__the_live_read_with_an_empty_timeline() -> None:
    setup = Setup([receipt(4242, created=NOW - timedelta(days=2))])

    one = await shop_order(setup.dashboard, auth(), SHOP, 4242, now=NOW)

    assert one.order.order_id == 4242
    assert one.freshness.source is DataSource.ETSY_LIVE
    assert one.timeline == ()


async def test_order__unknown__not_found() -> None:
    setup = await stored()

    with pytest.raises(OrderNotFoundError):
        await shop_order(setup.dashboard, auth(), SHOP, 999, now=NOW)


@pytest.mark.ac("AC-5.6.1")
async def test_order__another_organizations_shop__not_found() -> None:
    setup = await stored()

    with pytest.raises(ShopNotFoundError):
        await shop_order(setup.dashboard, auth(org=OTHER_ORG), SHOP, 100_000, now=NOW)


@pytest.mark.ac("AC-5.6.1")
async def test_order__a_shop_not_granted__not_found() -> None:
    setup = await stored()

    with pytest.raises(ShopNotFoundError):
        await shop_order(
            setup.dashboard, auth(Role.SUPPORT, shops=frozenset()), SHOP, 100_000, now=NOW
        )


@pytest.mark.ac("AC-5.6.2")
async def test_order__viewer__no_buyer_name_or_tracking() -> None:
    setup = await stored()

    one = await shop_order(setup.dashboard, auth(Role.VIEWER), SHOP, 100_000, now=NOW)

    assert one.buyer_details is False
    assert one.order.buyer is None
    assert all(s.tracking_code is None for s in one.order.shipments)
    assert setup.store.buyer_reads[-1] is False
