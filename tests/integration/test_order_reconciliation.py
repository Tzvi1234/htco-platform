"""Reconciliation's records against real Postgres (slice 5.5), as `htco_worker`: drifts with both
values recorded once, their event, and the cursor of the shop's last successful run."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest

from htco_application.order_ingest import DRIFT_DETECTED, IngestContext, IngestSource
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

RECONCILE = IngestContext(IngestSource.RECONCILIATION, correlation_id="corr-rec", detect_drift=True)
DRIFTS = "select count(*) from app.sync_drifts where shop_id = %s and receipt_id = %s"


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


@pytest.mark.ac("AC-5.5.4")
async def test_a_missing_order__one_drift_and_one_event_however_often_reconciled(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    lost = order(81, days_ago=2)  # older than the grace: its webhook never came
    store = PostgresOrderStore(worker_db)

    with tenant_scope(tenant.organization_id):
        for _ in range(3):
            await store.ingest_receipts(integration, [lost], context=RECONCILE, now=NOW)

    key = [integration.shop_id, 81]
    assert scalar(database, DRIFTS, key) == 1
    assert (
        scalar(
            database, "select kind from app.sync_drifts where shop_id = %s and receipt_id = %s", key
        )
        == "missing"
    )
    events = "select count(*) from jobs.outbox where payload ->> 'event_type' = %s and payload ->> 'receipt_id' = %s"  # noqa: E501 - one query
    assert scalar(database, events, [DRIFT_DETECTED, "81"]) == 1


@pytest.mark.ac("AC-5.5.4")
async def test_a_status_difference__recorded_with_both_values(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    first = order(82)
    store = PostgresOrderStore(worker_db)
    shipped = replace(
        first, status="completed", is_shipped=True, updated_at=first.updated_at + timedelta(hours=1)
    )

    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(
            integration, [first], context=IngestContext(IngestSource.WEBHOOK), now=NOW
        )
        await store.ingest_receipts(integration, [shipped], context=RECONCILE, now=NOW)

    with database.connection() as connection:
        row = connection.execute(
            "select stored, observed from app.sync_drifts where shop_id = %s and receipt_id = %s "
            "and kind = 'status'",
            [integration.shop_id, 82],
        ).fetchone()
    assert row is not None
    assert row[0] == {"status": "paid", "is_shipped": False}
    assert row[1] == {"status": "completed", "is_shipped": True}


async def test_a_matching_order__no_drift(database: Database, worker_db: Database) -> None:
    tenant, integration = await connected(database)
    receipt = order(83)
    store = PostgresOrderStore(worker_db)

    with tenant_scope(tenant.organization_id):
        await store.ingest_receipts(
            integration, [receipt], context=IngestContext(IngestSource.WEBHOOK), now=NOW
        )
        await store.ingest_receipts(integration, [receipt], context=RECONCILE, now=NOW)

    assert scalar(database, DRIFTS, [integration.shop_id, 83]) == 0


async def test_the_cursor__keeps_the_last_success_and_the_last_error(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    store = PostgresOrderStore(worker_db)

    with tenant_scope(tenant.organization_id):
        await store.record_cursor(integration, "reconcile", started_at=NOW, error=None)
        later = NOW + timedelta(minutes=10)
        await store.record_cursor(
            integration, "reconcile", started_at=later, error="ConnectionError"
        )

    with database.connection() as connection:
        row = connection.execute(
            "select last_started_at, last_succeeded_at, last_error from app.sync_cursors "
            "where shop_id = %s and kind = 'reconcile'",
            [integration.shop_id],
        ).fetchone()
    assert row == (later, NOW, "ConnectionError")
