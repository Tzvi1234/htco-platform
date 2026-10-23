"""The stored order history against real Postgres (owner's decision of 2026-10-22).

Idempotent upserts by `(shop_id, receipt_id)`, the backfill checkpoint, refunds with reasons,
buyer data readable only by buyer-data roles (row-level security), no column for contact data,
the advisory lock and the purge of disconnected shops. Every test makes its own tenant.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest

from htco_application.marketplace import (
    MarketplaceMoney,
    OrderLine,
    OrderReceipt,
    OrderRefund,
    OrderShipment,
    ShippingAddress,
)
from htco_application.shop_integrations import ShopIntegration
from htco_domain.orders import OrderState
from htco_persistence.database import Database
from htco_persistence.etsy_connect import PostgresShopConnector
from htco_persistence.etsy_orders import PostgresOrderStore
from tests.integration.test_etsy_connection import (
    NOW,
    Tenant,
    _url,
    etsy_ids,
    request_for,
    scalar,
    vault,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]

FULL_NAME = "Fakedb Middle Buyerqq"
TRACKING = "FAKE-DB-TRACK-01"
TABLES = (
    "etsy_orders",
    "etsy_order_items",
    "etsy_order_refunds",
    "etsy_order_buyers",
    "etsy_order_sync_state",
)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def usd(cents: int) -> MarketplaceMoney:
    return MarketplaceMoney(amount=cents, divisor=100, currency_code="USD")


def order(receipt_id: int, *, days_ago: int = 3, status: str = "paid") -> OrderReceipt:
    created = NOW - timedelta(days=days_ago)
    return OrderReceipt(
        receipt_id=receipt_id,
        status=status,
        is_paid=True,
        is_shipped=False,
        created_at=created,
        updated_at=created,
        grand_total=usd(2599),
        subtotal=usd(2000),
        shipping_total=usd(599),
        discount_total=usd(0),
        lines=(
            OrderLine(
                transaction_id=receipt_id * 10,
                listing_id=777,
                title="Fake db mug",
                sku="FAKE-DB",
                quantity=2,
                unit_price=usd(1000),
                is_digital=False,
                expected_ship_date=created + timedelta(days=3),
            ),
        ),
        shipments=(OrderShipment("fake-post", TRACKING, created + timedelta(days=1)),),
        buyer_display_name="Fakedb B.",
        country_code="US",
        buyer_name=FULL_NAME,
        shipping_address=ShippingAddress("FAKE-DB-LINE", None, "Fakedbcity", "FK", "FKZIP", "US"),
        refunds=(OrderRefund(usd(500), created, "damaged", "refunded", "FAKE-DB-NOTE"),),
    )


async def connected(database: Database) -> tuple[Tenant, ShopIntegration]:
    tenant = Tenant(database)
    the_vault = vault(database)
    shop = await PostgresShopConnector(database, the_vault).connect(request_for(tenant, etsy_ids()))
    integration = await the_vault.describe(shop.shop.id)
    assert integration is not None
    return tenant, integration


def count(database: Database, table: str, shop: uuid.UUID) -> int:
    return int(scalar(database, f"select count(*) from app.{table} where shop_id = %s", [shop]))  # noqa: S608 - fixed names


async def test_upsert__replayed__no_duplicate_orders_items_or_refunds(database: Database) -> None:
    _, integration = await connected(database)
    store = PostgresOrderStore(database)
    page = [order(1), order(2)]

    for _ in range(3):
        await store.save_backfill_page(integration, page, next_offset=2, now=NOW)

    shop = integration.shop_id
    assert [count(database, t, shop) for t in TABLES[:4]] == [2, 2, 2, 2]


async def test_upsert__newer_copy_updates_status_and_refunds__older_copy_ignored(
    database: Database,
) -> None:
    _, integration = await connected(database)
    store = PostgresOrderStore(database)
    first = order(5)
    await store.save_backfill_page(integration, [first], next_offset=None, now=NOW)

    later = first.updated_at + timedelta(hours=1)
    refunds = (
        *first.refunds,
        OrderRefund(usd(2099), later, "out_of_stock", "refunded", None),
    )
    newer = replace(first, status="fully refunded", updated_at=later, refunds=refunds)
    await store.save_incremental_page(integration, [newer], finished_at=NOW, now=NOW)
    await store.save_incremental_page(
        integration, [replace(first, status="paid")], finished_at=NOW, now=NOW
    )

    [stored] = await store.load(
        integration.shop_id, integration.etsy_shop_id, since=None, buyer_data=True
    )
    assert stored.status == "fully refunded"
    assert [(r.reason, r.amount.amount) for r in stored.refunds] == [
        ("damaged", 500),
        ("out_of_stock", 2099),
    ]
    assert count(database, "etsy_order_refunds", integration.shop_id) == 2


async def test_load__round_trip_with_and_without_buyer_data(database: Database) -> None:
    _, integration = await connected(database)
    store = PostgresOrderStore(database)
    original = order(9)
    await store.save_backfill_page(integration, [original], next_offset=None, now=NOW)

    [full] = await store.load(
        integration.shop_id, integration.etsy_shop_id, since=None, buyer_data=True
    )
    [bare] = await store.load(
        integration.shop_id, integration.etsy_shop_id, since=None, buyer_data=False
    )

    # The store adds what its state machine recorded (slice 5.6); everything else round-trips.
    assert full == replace(original, stored_state=OrderState.AWAITING_SHIPMENT)
    assert bare.buyer_name is None
    assert bare.buyer_display_name is None
    assert bare.shipping_address is None
    assert bare.shipments[0].tracking_code == ""
    assert bare.shipments[0].carrier == "fake-post"
    assert bare.refunds == original.refunds
    assert bare.lines == original.lines
    newer = await store.load(
        integration.shop_id, integration.etsy_shop_id, since=NOW, buyer_data=False
    )
    assert newer == ()


async def test_schema__no_column_can_hold_contact_data(database: Database) -> None:
    with database.connection() as connection:
        columns = connection.execute(
            """
            select table_name, column_name from information_schema.columns
            where table_schema = 'app' and table_name = any(%s)
            """,
            [list(TABLES)],
        ).fetchall()
    assert columns
    for table, column in columns:
        for word in ("email", "phone", "message", "gift"):
            assert word not in column, (table, column)


async def test_rls__buyer_rows_only_for_buyer_data_roles(database: Database) -> None:
    tenant, integration = await connected(database)
    store = PostgresOrderStore(database)
    await store.save_backfill_page(integration, [order(11)], next_offset=None, now=NOW)
    finance, finance_membership = uuid.uuid4(), uuid.uuid4()
    with database.connection() as connection:
        connection.execute(
            "insert into auth.users (id, email) values (%s, %s)",
            [finance, f"fin-{finance.hex[:12]}@example.test"],
        )
        connection.execute(
            "insert into app.memberships (id, organization_id, user_id, role) "
            "values (%s, %s, %s, 'finance')",
            [finance_membership, tenant.organization_id, finance],
        )
        for membership in (finance_membership, tenant.member_membership):
            connection.execute(
                "insert into app.shop_access (id, organization_id, membership_id, shop_id) "
                "values (%s, %s, %s, %s)",
                [uuid.uuid4(), tenant.organization_id, membership, integration.shop_id],
            )

    def seen(user: uuid.UUID, table: str) -> int:
        with database.user_transaction(tenant.claims(user)) as connection:
            row = connection.execute(f"select count(*) from app.{table}").fetchone()  # noqa: S608 - fixed names
        assert row is not None
        return int(row[0])

    stranger = Tenant(database)
    assert (seen(tenant.owner, "etsy_orders"), seen(tenant.owner, "etsy_order_buyers")) == (1, 1)
    # Operations with access to the shop may see buyer details.
    assert seen(tenant.member, "etsy_order_buyers") == 1
    assert (seen(finance, "etsy_orders"), seen(finance, "etsy_order_buyers")) == (1, 0)
    assert seen(finance, "etsy_order_refunds") == 1
    assert seen(stranger.owner, "etsy_orders") == 0
    with (
        pytest.raises(Exception, match="permission denied"),
        database.user_transaction(tenant.claims(tenant.owner)) as connection,
    ):
        connection.execute("delete from app.etsy_orders")


async def test_checkpoint__moves_with_each_page_and_completes(database: Database) -> None:
    _, integration = await connected(database)
    store = PostgresOrderStore(database)

    await store.save_backfill_page(integration, [order(21)], next_offset=100, now=NOW)
    middle = await store.state(integration.shop_id)
    await store.save_backfill_page(integration, [order(22)], next_offset=None, now=NOW)
    done = await store.state(integration.shop_id)
    await store.record_failure(integration.shop_id, error="ConnectionError", now=NOW)
    failed = await store.state(integration.shop_id)

    assert middle is not None
    assert (middle.backfill_offset, middle.backfill_completed_at, middle.orders_stored) == (
        100,
        None,
        1,
    )
    assert done is not None
    assert done.complete_for(integration.etsy_shop_id)
    assert done.orders_stored == 2
    assert failed is not None
    assert failed.last_error == "ConnectionError"


async def test_lock__one_holder_at_a_time(database: Database) -> None:
    _, integration = await connected(database)
    store = PostgresOrderStore(database)

    async with store.lock(integration.shop_id) as first, store.lock(integration.shop_id) as second:
        assert (first, second) == (True, False)
    async with store.lock(integration.shop_id) as again:
        assert again is True


async def test_purge__deletes_orders_of_shops_disconnected_past_their_date(
    database: Database,
) -> None:
    _, gone = await connected(database)
    _, kept = await connected(database)
    _, waiting = await connected(database)
    store = PostgresOrderStore(database)
    for integration, receipt_id in ((gone, 31), (kept, 32), (waiting, 33)):
        await store.save_backfill_page(integration, [order(receipt_id)], next_offset=None, now=NOW)
    the_vault = vault(database)
    await the_vault.disconnect(gone.shop_id, purge_after=NOW + timedelta(days=30))
    await the_vault.disconnect(waiting.shop_id, purge_after=NOW + timedelta(days=60))

    purged = await store.purge_disconnected(now=NOW + timedelta(days=31))

    assert purged >= 1
    assert [count(database, t, gone.shop_id) for t in TABLES] == [0, 0, 0, 0, 0]
    assert count(database, "etsy_orders", kept.shop_id) == 1
    assert count(database, "etsy_orders", waiting.shop_id) == 1
