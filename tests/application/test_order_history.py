"""The stored order history: backfill, incremental sync, DB-backed reads (all values invented).

The owner's decision of 2026-10-22: Orders and Statistics cover the whole history since the shop
opened and load from HTCO's database; the backfill pages through every receipt (no cap), resumes
after a crash, and replays change nothing.
"""

import uuid
from dataclasses import fields, replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from htco_application.kernel import Page
from htco_application.marketplace import (
    AccessTokenProvider,
    MarketplaceRateLimitedError,
    OrderReceipt,
    OrderRefund,
    ShippingAddress,
)
from htco_application.order_history import (
    FRESH_FOR,
    HistoryState,
    OrderSync,
    adapter_pages,
    request_order_sync,
)
from htco_application.shop_capabilities import ShopNotConnectedError
from htco_application.shop_dashboard import (
    RECEIPT_CAPS,
    DataSource,
    OrderPeriod,
    OrderViewState,
    shop_orders,
    shop_overview,
    shop_stats,
)
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationStatus
from tests.application.test_shop_dashboard import OTHER_ORG, SHOP, World, auth
from tests.etsy_fakes import ETSY_SHOP, Pinning
from tests.order_history_fakes import AuditLog, MemoryOrderStore
from tests.shop_data_fakes import NOW, TRACKING, ShopData, receipt, usd

pytestmark = pytest.mark.anyio

FULL_NAME = "Fakebuyer Middle Quuxley"
FAKE_LINE = "FAKE-STREET-LINE-ZZZ"


class Recording:
    """The shop data source, recording each receipts read's offset; `rate_limited` 429s first."""

    def __init__(self, source: ShopData, *, rate_limited: int = 0) -> None:
        self.source = source
        # (the window's start, whichever filter names it; the offset)
        self.offsets: list[tuple[datetime | None, int]] = []
        self.modified_since: list[datetime] = []
        self.rate_limited = rate_limited

    async def list_order_receipts(  # noqa: PLR0913 - the port's signature
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        tokens: AccessTokenProvider | None = None,
    ) -> Page[OrderReceipt]:
        if self.rate_limited:
            self.rate_limited -= 1
            raise MarketplaceRateLimitedError("fake 429", retry_after_seconds=2)
        self.offsets.append((min_created or min_last_modified, offset))
        if min_last_modified is not None:
            self.modified_since.append(min_last_modified)
        return await self.source.list_order_receipts(
            shop_id,
            min_created=min_created,
            min_last_modified=min_last_modified,
            limit=limit,
            offset=offset,
            urgent=urgent,
            tokens=tokens,
        )


def personal(r: OrderReceipt) -> OrderReceipt:
    return replace(
        r,
        buyer_name=FULL_NAME,
        shipping_address=ShippingAddress(FAKE_LINE, None, "Fakecity", None, "00000", "US"),
        country_code="US",
    )


def many(count: int, *, start_days: int = 1) -> list[OrderReceipt]:
    """`count` completed orders, one a day going back from `start_days` ago."""
    return [
        personal(
            receipt(
                100_000 + n,
                created=NOW - timedelta(days=start_days + n),
                status="completed",
                shipped=True,
            )
        )
        for n in range(count)
    ]


class Setup:
    def __init__(self, receipts: list[OrderReceipt], *, rate_limited: int = 0) -> None:
        self.world = World()
        self.world.source.receipts = receipts
        self.recording = Recording(self.world.source, rate_limited=rate_limited)
        self.store = MemoryOrderStore()
        self.audit = AuditLog()
        self.waits: list[float] = []

        async def sleep(seconds: float) -> None:
            self.waits.append(seconds)

        self.sync = OrderSync(
            store=self.store,
            pages=adapter_pages(self.recording, Pinning()),
            clock=self.world.clock,
            audit=self.audit,
            sleep=sleep,
        )
        self.dashboard = replace(self.world.dashboard, history=self.sync)

    @property
    def integration(self):  # type: ignore[no-untyped-def]
        return self.world.vault.rows[SHOP]


# --- Backfill ------------------------------------------------------------------------------------


async def test_backfill__pages_through_every_receipt_without_a_cap_then_the_last_30_days() -> None:
    setup = Setup(many(2_150))

    ran = await setup.sync.run(setup.integration)

    assert ran is True
    assert len(setup.store.orders) == 2_150  # more than the live read's cap of 2,000
    full = [offset for since, offset in setup.recording.offsets if since is None]
    assert full == list(range(0, 2_200, 100))
    assert [since for since, _ in setup.recording.offsets if since is not None] == [
        NOW - timedelta(days=30)
    ]
    state = await setup.store.state(SHOP)
    assert state is not None
    assert state.backfill_completed_at == NOW
    assert state.incremental_synced_at == NOW


async def test_backfill__replayed__stores_each_order_once() -> None:
    setup = Setup(many(250))
    await setup.sync.run(setup.integration)
    first = dict(setup.store.orders)

    await setup.sync.run(setup.integration)
    await setup.sync.run(setup.integration)

    assert setup.store.orders == first
    assert len(setup.store.orders) == 250


async def test_backfill__interrupted__resumes_from_the_checkpoint() -> None:
    setup = Setup(many(350))
    setup.store.crash_after_pages = 2

    with pytest.raises(ConnectionError):
        await setup.sync.run(setup.integration)
    state = await setup.store.state(SHOP)
    assert state is not None
    assert state.backfill_offset == 200
    assert state.backfill_completed_at is None

    setup.store.crash_after_pages = None
    setup.recording.offsets.clear()
    await setup.sync.run(setup.integration)

    full = [offset for since, offset in setup.recording.offsets if since is None]
    assert full == [200, 300]  # not from the start again
    assert len(setup.store.orders) == 350


async def test_backfill__rate_limited__waits_and_retries_the_same_page() -> None:
    setup = Setup(many(5), rate_limited=2)

    await setup.sync.run(setup.integration)

    assert setup.waits == [2, 2]
    assert len(setup.store.orders) == 5


async def test_run__another_instance_holds_the_lock__no_etsy_call() -> None:
    setup = Setup(many(5))
    setup.store.held.add(SHOP)

    ran = await setup.sync.run(setup.integration)

    assert ran is False
    assert setup.recording.offsets == []


# --- Incremental ---------------------------------------------------------------------------------


async def test_incremental__status_change_refund_and_shipment__update_the_stored_order() -> None:
    first = personal(receipt(7001, created=NOW - timedelta(days=3)))
    setup = Setup([first])
    await setup.sync.run(setup.integration)

    later = NOW + timedelta(minutes=20)
    setup.world.clock.now = later
    refund = OrderRefund(usd(2400), later, "out_of_stock", "refunded", note_from_issuer="Fake")
    setup.world.source.receipts = [
        replace(first, status="fully refunded", updated_at=later, refunds=(refund,))
    ]
    await setup.sync.run(setup.integration)

    stored = setup.store.orders[SHOP, 7001]
    assert stored.status == "fully refunded"
    assert stored.refunds == (refund,)
    assert len(setup.store.orders) == 1


async def test_incremental__a_late_refund_of_an_order_older_than_30_days__reaches_the_store() -> (
    None
):
    """Post-merge review: the incremental sync read receipts *created* in the last 30 days, so a
    refund or cancellation of an older order never reached the stored history (Statistics kept
    counting it as a sale). It reads the receipts *changed* in the last 30 days."""
    old = personal(receipt(7003, created=NOW - timedelta(days=45)))
    setup = Setup([old])
    await setup.sync.run(setup.integration)

    later = NOW + timedelta(minutes=20)
    setup.world.clock.now = later
    refund = OrderRefund(usd(2400), later, "damaged", "refunded")
    setup.world.source.receipts = [
        replace(old, status="fully refunded", updated_at=later, refunds=(refund,))
    ]
    setup.recording.modified_since.clear()
    await setup.sync.run(setup.integration)

    assert setup.recording.modified_since == [later - timedelta(days=30)]
    stored = setup.store.orders[SHOP, 7003]
    assert stored.status == "fully refunded"
    assert stored.refunds == (refund,)


async def test_incremental__an_older_copy__never_overwrites_a_newer_one() -> None:
    newer = receipt(7002, created=NOW - timedelta(days=2), status="completed", shipped=True)
    newer = replace(newer, updated_at=NOW)
    setup = Setup([newer])
    await setup.sync.run(setup.integration)

    setup.world.source.receipts = [
        replace(newer, status="paid", updated_at=NOW - timedelta(days=1))
    ]
    await setup.sync.run(setup.integration)

    assert setup.store.orders[SHOP, 7002].status == "completed"


# --- Triggers ------------------------------------------------------------------------------------


async def test_reads__start_the_sync_in_the_background_and_serve_live_meanwhile() -> None:
    setup = Setup(many(3))

    page = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)

    assert page.freshness.source is DataSource.ETSY_LIVE
    assert page.sync is not None
    assert page.sync.state is HistoryState.SYNCING_HISTORY
    await setup.sync.idle()
    after = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    assert after.freshness.source is DataSource.HTCO_STORE
    assert after.sync is not None
    assert after.sync.state is HistoryState.COMPLETE
    assert after.sync.orders_stored == 3


async def test_overview__starts_the_sync_too() -> None:
    setup = Setup(many(3))

    await shop_overview(setup.dashboard, auth(), SHOP, now=NOW)
    await setup.sync.idle()

    state = await setup.store.state(SHOP)
    assert state is not None
    assert state.backfill_completed_at is not None


async def test_reads__fresh_history__no_new_sync_until_10_minutes_pass() -> None:
    setup = Setup(many(3))
    await setup.sync.run(setup.integration)
    setup.recording.offsets.clear()

    await shop_orders(setup.dashboard, auth(), SHOP, now=NOW + FRESH_FOR / 2)
    await setup.sync.idle()
    assert setup.recording.offsets == []

    setup.world.clock.now = NOW + FRESH_FOR
    await shop_orders(setup.dashboard, auth(), SHOP, now=NOW + FRESH_FOR)
    await setup.sync.idle()
    assert [since for since, _ in setup.recording.offsets] == [NOW + FRESH_FOR - timedelta(days=30)]


async def test_a_failed_run__records_the_error_type_only_and_is_not_retried_at_once() -> None:
    setup = Setup(many(3))
    await setup.store.save_incremental_page(setup.integration, [], finished_at=None, now=NOW)
    setup.store.crash_after_pages = 0

    await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)
    await setup.sync.idle()
    await shop_orders(setup.dashboard, auth(), SHOP, now=NOW + timedelta(seconds=5))

    assert not setup.sync.running(SHOP)
    assert setup.store.states[SHOP].last_error == "ConnectionError"


# --- Reads from the store ------------------------------------------------------------------------


async def test_orders_from_the_store__every_period_whole__not_partial() -> None:
    setup = Setup(many(2_100))
    await setup.sync.run(setup.integration)
    setup.recording.offsets.clear()

    every = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW, period=OrderPeriod.ALL)
    month = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)

    assert every.total == 2_100
    assert every.partial is False
    assert every.cap == RECEIPT_CAPS[OrderPeriod.ALL]
    assert every.freshness.source is DataSource.HTCO_STORE
    assert every.freshness.stale is False
    assert month.total == 30  # 1 to 30 days ago (the period's first moment included)
    assert setup.recording.offsets == []  # no receipts read from Etsy


async def test_orders_from_the_store__buyer_details_only_for_buyer_data_roles() -> None:
    shipped = personal(
        receipt(
            7101,
            created=NOW - timedelta(days=2),
            status="completed",
            shipped=True,
            shipments=(),
        )
    )
    from htco_application.marketplace import OrderShipment  # noqa: PLC0415 - local fixture

    shipped = replace(
        shipped, shipments=(OrderShipment("fake-post", TRACKING, NOW - timedelta(days=1)),)
    )
    setup = Setup([shipped])
    await setup.sync.run(setup.integration)

    owner = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)
    finance = await shop_orders(setup.dashboard, auth(Role.FINANCE), SHOP, now=NOW)

    [seen] = owner.items
    assert seen.buyer == "Fakebuyer Q."  # the display name, never the full name
    assert seen.shipments[0].tracking_code == TRACKING
    [hidden] = finance.items
    assert hidden.buyer is None
    assert hidden.shipments[0].tracking_code is None
    assert setup.store.buyer_reads[-1] is False  # the buyer table was not even read
    names = {f.name for f in fields(seen)}
    assert not names & {"shipping_address", "address", "buyer_name"}
    text = repr(owner) + repr(finance)
    for secret in (FULL_NAME, FAKE_LINE, "Fakecity"):
        assert secret not in text


async def test_orders_from_the_store__refunds_with_reason_and_total() -> None:
    refund = OrderRefund(usd(1000), NOW - timedelta(days=1), "damaged", "refunded", "Fake note")
    setup = Setup(
        [
            replace(
                receipt(7201, created=NOW - timedelta(days=4), status="partially refunded"),
                refunds=(refund,),
            )
        ]
    )
    await setup.sync.run(setup.integration)

    owner = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)
    finance = await shop_orders(setup.dashboard, auth(Role.FINANCE), SHOP, now=NOW)

    [order] = owner.items
    assert order.state is OrderViewState.PAID
    [view] = order.refunds
    assert (view.reason, view.status, view.note) == ("damaged", "refunded", "Fake note")
    assert order.refunded is not None
    assert order.refunded.amount == Decimal("10.00")
    assert finance.items[0].refunds[0].note is None  # the note is for buyer-data roles


async def test_stats_from_the_store__whole_history_customers_and_reasons() -> None:
    a = personal(receipt(7301, created=NOW - timedelta(days=900), status="completed"))
    b = personal(receipt(7302, created=NOW - timedelta(days=10), status="completed"))
    c = replace(
        receipt(7303, created=NOW - timedelta(days=5), status="completed"),
        buyer_name="Otherfake Buyer",
        country_code="DE",
    )
    cancelled = replace(
        receipt(7304, created=NOW - timedelta(days=3), status="canceled"),
        refunds=(OrderRefund(usd(2400), NOW, "buyer_canceled", "refunded"),),
    )
    setup = Setup([a, b, c, cancelled])
    await setup.sync.run(setup.integration)

    stats = await shop_stats(setup.dashboard, auth(Role.VIEWER), SHOP, now=NOW)

    assert stats.freshness.source is DataSource.HTCO_STORE
    sales = stats.sales
    assert sales is not None
    assert sales.receipts_read == 4
    assert sales.partial is False
    assert sales.orders == 3
    assert sales.customers == 2
    assert sales.repeat_customers_percent == Decimal("50.0")
    [reason] = sales.refunds_and_cancellations
    assert (reason.kind, reason.reason, reason.orders, reason.amount) == (
        "cancellation",
        "buyer_canceled",
        1,
        Decimal("24.00"),
    )
    assert FULL_NAME not in repr(stats)
    assert "Otherfake" not in repr(stats)


async def test_store_unreadable__falls_back_to_the_live_read() -> None:
    setup = Setup(many(3))
    await setup.sync.run(setup.integration)

    async def broken(*args: object, **kwargs: object) -> tuple[OrderReceipt, ...]:  # noqa: ARG001
        raise ConnectionError("fake")

    setup.store.load = broken  # type: ignore[method-assign]

    page = await shop_orders(setup.dashboard, auth(), SHOP, now=NOW)

    assert page.freshness.source is DataSource.ETSY_LIVE


# --- Manual sync ---------------------------------------------------------------------------------


async def test_manual_sync__owner__starts_it_and_audits_without_buyer_data() -> None:
    setup = Setup(many(3))

    sync = await request_order_sync(
        setup.sync, setup.world.vault, auth(), SHOP, now=NOW, correlation_id="req-1"
    )
    await setup.sync.idle()

    assert sync.state is HistoryState.SYNCING_HISTORY
    assert len(setup.store.orders) == 3
    [record] = setup.audit.records
    assert record.action == "shop.order_sync_requested"  # type: ignore[attr-defined]
    assert record.after == {"started": True}  # type: ignore[attr-defined]
    assert FULL_NAME not in repr(record.after)  # type: ignore[attr-defined]


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.FINANCE, Role.VIEWER])
async def test_manual_sync__not_owner_or_admin__refused(role: Role) -> None:
    setup = Setup(many(3))

    with pytest.raises(ShopAccessDeniedError):
        await request_order_sync(
            setup.sync, setup.world.vault, auth(role), SHOP, now=NOW, correlation_id=None
        )
    assert setup.audit.records == []


async def test_manual_sync__another_organization__not_found() -> None:
    setup = Setup(many(3))

    with pytest.raises(ShopNotFoundError):
        await request_order_sync(
            setup.sync, setup.world.vault, auth(org=OTHER_ORG), SHOP, now=NOW, correlation_id=None
        )
    with pytest.raises(ShopNotFoundError):
        await request_order_sync(
            setup.sync, setup.world.vault, auth(), uuid.uuid4(), now=NOW, correlation_id=None
        )


async def test_manual_sync__disconnected__not_connected() -> None:
    setup = Setup(many(3))
    setup.world.set_status(IntegrationStatus.DISCONNECTED)

    with pytest.raises(ShopNotConnectedError):
        await request_order_sync(
            setup.sync, setup.world.vault, auth(), SHOP, now=NOW, correlation_id=None
        )


def test_etsy_shop_id_fixture_is_the_integrations() -> None:
    assert Setup(many(1)).integration.etsy_shop_id == ETSY_SHOP
