"""The worker's `etsy.webhook_event` job (slice 5.3): wired to the real adapter, it re-reads the
listing and its stock with GETs only (a strict mock fails the test on any other method)."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_application.etsy_webhooks import (
    INVENTORY_CHANGED,
    OUT_OF_STOCK,
    WEBHOOK_JOB,
    EngineOrderIntake,
    WebhookProcessor,
)
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_integrations.etsy import EtsyConfig
from htco_worker.main import webhook_handlers
from tests.application.test_etsy_webhooks import FakeEngine
from tests.integrations.test_etsy_adapter import SHOP_ID, Harness, fixture, ok
from tests.integrations.test_etsy_receipt_history import _Routed
from tests.webhook_fakes import NOW, ORG, SHOP, KnownShops, MemoryOutbox, MemoryWebhookInbox

pytestmark = pytest.mark.anyio


def _job() -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid7(), kind=WEBHOOK_JOB, idempotency_key="k",
        payload={"event_type": "inventory.out_of_stock", "shop_id": SHOP_ID,
                 "listing_id": 1500000001, "product_id": 2500000001, "webhook_id": "msg_fake1"},
        organization_id=ORG, shop_id=SHOP, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW + timedelta(minutes=1), correlation_id="etsy-webhook:msg_fake1",
    )  # fmt: skip


@pytest.mark.ac("AC-5.3.4")
async def test_webhook_job__rereads_through_the_adapter_with_gets_only() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inventory"):
            return ok(fixture("listing_inventory.json"))
        return ok(fixture("listing.json"))

    etsy = _Routed(answer)
    harness = Harness(etsy, EtsyConfig())
    outbox = MemoryOutbox()
    processor = WebhookProcessor(
        orders=EngineOrderIntake(FakeEngine()),
        catalog=harness.adapter,
        shops=KnownShops(),
        outbox=outbox,
        marks=MemoryWebhookInbox(),
        clock=lambda: NOW,
    )
    handlers = webhook_handlers(processor)

    async with harness.client:
        await handlers[WEBHOOK_JOB](_job())

    assert [r.method for r in etsy.requests] == ["GET", "GET"]
    assert {m.payload["event_type"] for m in outbox.messages.values()} == {
        INVENTORY_CHANGED,
        OUT_OF_STOCK,
    }
