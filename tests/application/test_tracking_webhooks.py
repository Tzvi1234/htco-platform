"""`order.shipped` also starts Tracking Synchronization (slice 6.1), beside Etsy Order Intake."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import override

import pytest

from htco_application.automation.engine import TriggerRequest
from htco_application.etsy_webhooks import ORDER_INTAKE, EngineOrderIntake, WebhookProcessor
from htco_application.fulfillment_schedule import TRACKING_SYNC
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.marketplace import OrderShipment
from htco_application.tracking_sync import EngineTrackingSync
from htco_domain.automation import AutomationExecution
from tests.application.test_etsy_webhooks import RECEIPT, Processing
from tests.application.test_execution_router import FakeMarketplace
from tests.application.test_tracking_sync import Sync
from tests.order_intake_fakes import paid_receipt, shipped
from tests.webhook_fakes import NOW, SHOP

pytestmark = pytest.mark.anyio


class WithTracking(Processing):
    @property
    @override
    def processor(self) -> WebhookProcessor:
        return replace(super().processor, tracking=EngineTrackingSync(self.engine))


@pytest.mark.ac("AC-6.7.2")
async def test_order_shipped_starts_intake_and_tracking_sync_once_each() -> None:
    run = WithTracking()

    outcome, _ = await run.run("order.shipped")

    assert outcome == "ingest_triggered"
    started = {r.automation_id: r.idempotency_key for r in run.engine.requests}
    assert started == {
        ORDER_INTAKE: f"etsy:{SHOP}:{RECEIPT}:order.shipped:etsy-webhook:msg_fake0001",
        TRACKING_SYNC: f"tracking:{SHOP}:{RECEIPT}:order.shipped:etsy-webhook:msg_fake0001",
    }


@pytest.mark.parametrize("event_type", ["order.paid", "order.canceled", "order.delivered"])
async def test_other_order_events_start_only_the_intake(event_type: str) -> None:
    run = WithTracking()

    await run.run(event_type)

    assert [r.automation_id for r in run.engine.requests] == [ORDER_INTAKE]


@dataclass
class OnceEngine:
    """The durable engine's rule: one execution per idempotency key. Tracking runs are kept to
    be run by the test (the handler on its own in-memory world)."""

    keys: set[str] = field(default_factory=set)
    tracking: list[TriggerRequest] = field(default_factory=list)

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        if request.idempotency_key not in self.keys:
            self.keys.add(request.idempotency_key)
            if request.automation_id == TRACKING_SYNC:
                self.tracking.append(request)
        return None  # type: ignore[return-value]


async def test_a_second_parcel_announced_after_the_first_job_completed_is_recorded() -> None:
    """Codex post-merge review (owner gate v4): the first `order.shipped` job completes, then
    Etsy announces a second parcel with another webhook ID. It gets its own job, intake and
    tracking run; the second parcel is read and recorded; a replay of it adds nothing."""
    first_parcel = shipped(paid_receipt(RECEIPT), minutes=9)
    sync = Sync(FakeMarketplace(order_receipts=(first_parcel,)))
    engine = OnceEngine()
    run = Processing()
    processor = replace(
        run.processor, orders=EngineOrderIntake(engine), tracking=EngineTrackingSync(engine)
    )

    async def deliver(webhook_id: str) -> None:
        accepted = await run.world.send("order.shipped", webhook_id=webhook_id)
        if accepted.duplicate or accepted.job_id is None:
            return
        stored = run.world.inbox.queue.jobs[accepted.job_id]
        await processor.process(
            ClaimedJob(
                id=stored.id,
                kind=stored.spec.kind,
                idempotency_key=stored.spec.idempotency_key,
                payload=stored.spec.payload,
                organization_id=stored.spec.organization_id,
                shop_id=stored.spec.shop_id,
                lock_key=None,
                attempts=1,
                retry=DEFAULT_RETRY,
                lease_expires_at=NOW + timedelta(minutes=1),
                correlation_id=stored.spec.correlation_id,
            )
        )
        for request in engine.tracking:
            context = replace(
                sync.context(dict(request.payload)),
                execution_id=uuid.uuid7(),
                correlation_id=request.correlation_id,
            )
            await sync.handler(context)
        engine.tracking.clear()

    await deliver("msg_parcel_1")
    assert len(sync.tracking.rows) == 1
    # The second parcel reaches Etsy after the first job completed.
    sync.marketplace.order_receipts = (
        replace(
            first_parcel,
            shipments=(
                *first_parcel.shipments,
                OrderShipment("Yun Express", "FAKE-INTAKE-TRACK-02", first_parcel.created_at),
            ),
        ),
    )
    await deliver("msg_parcel_2")
    await deliver("msg_parcel_2")  # Etsy's retry of the same delivery

    assert len(sync.tracking.rows) == 2  # the second parcel is recorded
    assert len(run.world.inbox.queue.jobs) == 2
    assert len(run.world.inbox.processed) == 2  # both inbox events are processed
    intake = sorted(k for k in engine.keys if k.startswith("etsy:"))
    assert intake == [
        f"etsy:{SHOP}:{RECEIPT}:order.shipped:etsy-webhook:msg_parcel_1",
        f"etsy:{SHOP}:{RECEIPT}:order.shipped:etsy-webhook:msg_parcel_2",
    ]
