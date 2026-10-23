"""Etsy Order Intake's writes against real Postgres (slice 5.4), as the worker's role in its
job's organization: one order per receipt however often it arrives, older copies ignored, every
transition recorded, exact money, events once, and one order store shared with the #57 sync."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from htco_application.order_ingest import INGESTED, IngestContext, IngestSource
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

WEBHOOK = IngestContext(IngestSource.WEBHOOK, correlation_id="corr-db-intake")


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def count(database: Database, sql: str, params: list[object]) -> int:
    return int(scalar(database, sql, params))


@pytest.mark.ac("AC-5.4.1")
async def test_worker__the_same_receipt_ten_times__one_order_one_event_one_history(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    receipt = order(71)
    store = PostgresOrderStore(worker_db)

    with tenant_scope(tenant.organization_id):
        for _ in range(10):
            await store.ingest_receipts(integration, [receipt], context=WEBHOOK, now=NOW)

    shop = integration.shop_id
    key = [shop, 71]
    assert (
        count(
            database,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == 1
    )
    assert (
        count(
            database,
            "select sum(quantity) from app.etsy_order_items where shop_id = %s and receipt_id = %s",
            key,
        )
        == 2
    )
    assert (
        count(
            database,
            "select count(*) from app.order_status_events where shop_id = %s and receipt_id = %s",
            key,
        )
        == 3
    )
    assert (
        count(
            database,
            "select count(*) from jobs.outbox where idempotency_key = %s",
            [f"{INGESTED}:{shop}:71"],
        )
        == 1
    )
    assert (
        scalar(
            database,
            "select order_state from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == "awaiting_shipment"
    )
    assert (
        scalar(
            database,
            "select buyer_name from app.etsy_order_buyers where shop_id = %s and receipt_id = %s",
            key,
        )
        is not None
    )
    revenue = scalar(
        database,
        "select amount from app.financial_entries where shop_id = %s and receipt_id = %s and kind = 'revenue'",  # noqa: E501 - one query per line
        key,
    )
    assert revenue == Decimal("25.99")  # fmt: skip


@pytest.mark.ac("AC-5.4.2")
async def test_worker__an_older_copy_after_a_newer_one__changes_nothing(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    first = order(72)
    newer = replace(
        first, status="completed", is_shipped=True, updated_at=first.updated_at + timedelta(hours=2)
    )
    store = PostgresOrderStore(worker_db)

    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(integration, [newer], context=WEBHOOK, now=NOW)
        [stale] = await store.ingest_receipts(integration, [first], context=WEBHOOK, now=NOW)

    assert not stale.write
    key = [integration.shop_id, 72]
    assert (
        scalar(
            database,
            "select status from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == "completed"
    )


@pytest.mark.ac("AC-5.4.6")
async def test_one_store__the_api_sync_and_the_worker_write_the_same_order(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    first = order(73)
    await PostgresOrderStore(database).save_backfill_page(
        integration, [first], next_offset=None, now=NOW
    )
    shipped = replace(
        first, status="completed", is_shipped=True, updated_at=first.updated_at + timedelta(hours=1)
    )

    with tenant_scope(tenant.organization_id):
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [shipped], context=WEBHOOK, now=NOW
        )

    key = [integration.shop_id, 73]
    assert (
        count(
            database,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == 1
    )
    states = "select to_state from app.order_status_events where shop_id = %s and receipt_id = %s order by created_at, id"  # noqa: E501 - one query per line
    with database.connection() as connection:
        rows = [r[0] for r in connection.execute(states, key).fetchall()]
    assert rows[-1] == "shipped"  # fmt: skip


@pytest.mark.ac("AC-5.4.6")
async def test_schema__no_second_order_table_exists(database: Database) -> None:
    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                select table_name from information_schema.tables
                where table_schema = 'app' and table_name like '%%order%%'
                """
            ).fetchall()
        }
    assert tables == {
        "etsy_orders",
        "etsy_order_items",
        "etsy_order_refunds",
        "etsy_order_buyers",
        "etsy_order_sync_state",
        "order_status_events",
    }


@pytest.mark.ac("AC-5.4.6")
async def test_schema__no_column_can_hold_contact_data(database: Database) -> None:
    with database.connection() as connection:
        columns = [
            row[0]
            for row in connection.execute(
                """
                select column_name from information_schema.columns
                where table_schema = 'app'
                  and (table_name like 'etsy_order%%' or table_name in
                       ('order_status_events', 'financial_entries'))
                """
            ).fetchall()
        ]
    for column in columns:
        for word in ("email", "phone", "message", "gift"):
            assert word not in column


async def test_worker__another_organizations_scope_writes_nothing(
    database: Database, worker_db: Database
) -> None:
    _, integration = await connected(database)

    with tenant_scope(uuid.uuid4()), pytest.raises(Exception):  # noqa: B017, PT011 - RLS refuses
        await PostgresOrderStore(worker_db).ingest_receipts(
            integration, [order(74)], context=WEBHOOK, now=NOW
        )

    key = [integration.shop_id, 74]
    assert (
        count(
            database,
            "select count(*) from app.etsy_orders where shop_id = %s and receipt_id = %s",
            key,
        )
        == 0
    )
