"""`order.shipped` also starts Tracking Synchronization (slice 6.1), beside Etsy Order Intake."""

from dataclasses import replace
from typing import override

import pytest

from htco_application.etsy_webhooks import ORDER_INTAKE, WebhookProcessor
from htco_application.fulfillment_schedule import TRACKING_SYNC
from htco_application.tracking_sync import EngineTrackingSync
from tests.application.test_etsy_webhooks import RECEIPT, Processing
from tests.webhook_fakes import SHOP

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
        ORDER_INTAKE: f"etsy:{SHOP}:{RECEIPT}:order.shipped",
        TRACKING_SYNC: f"tracking:{SHOP}:{RECEIPT}:order.shipped:etsy-webhook:msg_fake0001",
    }


@pytest.mark.parametrize("event_type", ["order.paid", "order.canceled", "order.delivered"])
async def test_other_order_events_start_only_the_intake(event_type: str) -> None:
    run = WithTracking()

    await run.run(event_type)

    assert [r.automation_id for r in run.engine.requests] == [ORDER_INTAKE]
