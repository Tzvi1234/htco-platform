"""Etsy webhooks against real Postgres (slice 5.3): the shop a webhook names, events kept with
and without a job, the processed mark, and Etsy Order Intake triggered by the worker's role."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anyio
import pytest

from htco_application.automation.durable import DurableAutomationEngine
from htco_application.automation.engine import TriggerRequest
from htco_application.etsy_webhooks import Accepted, EngineOrderIntake, EtsyWebhookIngress
from htco_application.maintenance import TenantShop
from htco_domain.automation import AutomationExecution
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_connect import PostgresShopConnector
from htco_persistence.etsy_webhooks import (
    PostgresWebhookInbox,
    PostgresWebhookShops,
    PostgresWorkerShops,
)
from tests.automation_fakes import EffectBook
from tests.integration.test_durable_engine import engine_on, register
from tests.integration.test_etsy_connection import (
    Tenant,
    _url,
    etsy_ids,
    request_for,
    scalar,
    vault,
)
from tests.webhook_fakes import SECRET, body_of, recorded_events, signed

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
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


async def connected(admin: Database) -> tuple[TenantShop, int]:
    tenant = Tenant(admin)
    ids = etsy_ids()
    shop = await PostgresShopConnector(admin, vault(admin)).connect(request_for(tenant, ids))
    return TenantShop(tenant.organization_id, shop.shop.id), ids[0]


def ingress(admin: Database) -> EtsyWebhookIngress:
    return EtsyWebhookIngress(
        secret=SECRET,
        inbox=PostgresWebhookInbox(admin),
        shops=PostgresWebhookShops(admin),
        clock=lambda: NOW,
        offload=anyio.to_thread.run_sync,
    )


async def send(admin: Database, etsy_shop: int, kind: str, webhook_id: str) -> Accepted:
    body = body_of(recorded_events()[kind] | {"shop_id": etsy_shop})
    return await ingress(admin).receive(signed(body, webhook_id=webhook_id, at=NOW), body)


@pytest.mark.ac("AC-5.3.4")
async def test_webhook__routed_to_the_connected_shop_with_one_job(admin: Database) -> None:
    shop, etsy_shop = await connected(admin)
    webhook_id = f"msg_{uuid.uuid4().hex[:12]}"

    first = await send(admin, etsy_shop, "order.paid", webhook_id)
    replay = await send(admin, etsy_shop, "order.paid", webhook_id)
    other = await send(admin, etsy_shop, "order.paid", webhook_id + "b")

    assert first.job_id is not None
    assert replay.duplicate
    assert other.job_id == first.job_id  # the same receipt and event type: one job
    row = "select organization_id, shop_id from jobs.jobs where id = %s"
    with admin.connection() as connection:
        assert connection.execute(row, [first.job_id]).fetchone() == (
            shop.organization_id,
            shop.shop_id,
        )


@pytest.mark.ac("AC-5.3.4")
async def test_webhook__unknown_type_or_shop__stored_with_no_job(admin: Database) -> None:
    _, etsy_shop = await connected(admin)
    webhook_id = f"msg_{uuid.uuid4().hex[:12]}"
    body = body_of({"event_type": "shop.renamed", "shop_id": etsy_shop})

    accepted = await ingress(admin).receive(signed(body, webhook_id=webhook_id, at=NOW), body)
    again = await ingress(admin).receive(signed(body, webhook_id=webhook_id, at=NOW), body)
    unknown = await send(admin, 999_999_999_999, "order.paid", webhook_id + "u")

    assert accepted.job_id is None
    assert again.duplicate
    assert unknown.job_id is None
    stored = "select count(*) from jobs.inbox where event_id = %s and job_id is null"
    assert scalar(admin, stored, [webhook_id]) == 1
    assert scalar(admin, stored, [webhook_id + "u"]) == 1


async def test_worker__marks_its_events_processed_and_reads_its_shop(
    admin: Database, worker_db: Database
) -> None:
    shop, etsy_shop = await connected(admin)
    webhook_id = f"msg_{uuid.uuid4().hex[:12]}"
    accepted = await send(admin, etsy_shop, "listing.updated", webhook_id)
    assert accepted.job_id is not None

    with tenant_scope(shop.organization_id):
        marked = PostgresWebhookInbox(worker_db).mark_processed(accepted.job_id, now=NOW)
        again = PostgresWebhookInbox(worker_db).mark_processed(accepted.job_id, now=NOW)
        found = await PostgresWorkerShops(worker_db).etsy_shop_of(shop)
    with tenant_scope(uuid.uuid4()):
        hidden = await PostgresWorkerShops(worker_db).etsy_shop_of(shop)

    assert (marked, again) == (1, 0)
    assert found == etsy_shop
    assert hidden is None  # another organization's job sees no shop


async def test_worker__triggers_order_intake_in_its_jobs_organization(
    admin: Database, worker_db: Database
) -> None:
    shop, _ = await connected(admin)
    automation_id = register(admin)  # an active stand-in: Etsy Order Intake is draft until 5.11
    engine = engine_on(worker_db, automation_id, EffectBook().handle)
    trigger = EngineOrderIntake(_Renamed(engine, automation_id), offload=anyio.to_thread.run_sync)

    with tenant_scope(shop.organization_id):
        results = [
            await trigger.receipt_event(
                shop,
                receipt_id=4000000001,
                event_type="order.paid",
                correlation_id="etsy-webhook:msg_db",
                causation_id="job:db",
            )
            for _ in range(2)
        ]

    assert results == [True, True]
    rows = "select count(*) from app.automation_executions where automation_id = %s"
    assert scalar(admin, rows, [automation_id]) == 1


@dataclass
class _Renamed:
    """Points the intake trigger at the stand-in automation."""

    engine: DurableAutomationEngine
    automation_id: str

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        return self.engine.trigger(replace(request, automation_id=self.automation_id))
