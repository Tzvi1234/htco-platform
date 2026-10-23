"""Slice 5.6 against real Postgres: the API's reads of one stored order and its timeline."""

import uuid
from collections.abc import AsyncIterator

import pytest

from htco_application.order_ingest import IngestContext, IngestSource
from htco_domain.orders import OrderState
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from tests.integration.test_etsy_connection import NOW, _url
from tests.integration.test_order_history import connected, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

WEBHOOK = IngestContext(IngestSource.WEBHOOK, correlation_id="corr-db-read")


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.mark.ac("AC-5.6.4")
@pytest.mark.ac("AC-5.6.5")
async def test_one_order__its_stored_state_and_its_transitions_oldest_first(
    database: Database,
) -> None:
    tenant, integration = await connected(database)
    store = PostgresOrderStore(database)
    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(integration, [order(81), order(82)], context=WEBHOOK, now=NOW)

    found = await store.load_order(
        integration.shop_id, integration.etsy_shop_id, 81, buyer_data=False
    )
    timeline = await store.timeline(integration.shop_id, 81)

    assert found is not None
    assert found.receipt_id == 81
    assert found.stored_state is OrderState.AWAITING_SHIPMENT
    assert found.buyer_name is None  # no buyer data without the flag
    assert [(c.from_state, c.to_state, c.source) for c in timeline] == [
        (None, OrderState.PAID, IngestSource.WEBHOOK),
        (OrderState.PAID, OrderState.INGESTED, IngestSource.WEBHOOK),
        (OrderState.INGESTED, OrderState.AWAITING_SHIPMENT, IngestSource.WEBHOOK),
    ]


async def test_one_order__unknown_or_another_shop__none(database: Database) -> None:
    tenant, integration = await connected(database)
    store = PostgresOrderStore(database)
    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(integration, [order(83)], context=WEBHOOK, now=NOW)

    assert (
        await store.load_order(integration.shop_id, integration.etsy_shop_id, 999, buyer_data=True)
        is None
    )
    assert (
        await store.load_order(uuid.uuid4(), integration.etsy_shop_id, 83, buyer_data=True) is None
    )
    assert await store.timeline(uuid.uuid4(), 83) == ()


async def test_the_window__every_stored_order_carries_its_state(database: Database) -> None:
    tenant, integration = await connected(database)
    store = PostgresOrderStore(database)
    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(integration, [order(84)], context=WEBHOOK, now=NOW)

    every = await store.load(integration.shop_id, integration.etsy_shop_id, since=None,
                             buyer_data=False)  # fmt: skip

    assert {r.receipt_id: r.stored_state for r in every}[84] is OrderState.AWAITING_SHIPMENT
