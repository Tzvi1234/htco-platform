"""Reconciliation on the queue (slice 5.5): every 10 minutes and nightly, Etsy Order Intake
re-reads the last 30 days and every paid, unshipped order, records drifts and keeps the store
complete without webhooks. Real gateway and API channel with in-memory stores; invented values."""

import json
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, override

import pytest

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import ActionOutcome, ActionRequest, Channel
from htco_application.execution.guards import Guards, MemoryBreakers, MemoryRateLimiter, MemorySlots
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.maintenance import TenantShop
from htco_application.marketplace import OrderReceipt, Page
from htco_application.order_ingest import (
    DRIFT_DETECTED,
    DriftKind,
    IngestContext,
    IngestSource,
    StoredOrder,
    detect_drifts,
    observed_facts,
    plan_ingest,
)
from htco_application.order_intake import (
    ORDER_SYNC_JOB,
    RECEIPTS_READ,
    RECONCILE,
    RECONCILE_SCHEDULE,
    SWEEP,
    SWEEP_SCHEDULE,
    daily_etsy_calls,
    enqueue_reconciliations,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.orders import OrderState
from tests.application.test_action_gateway import ORG, SHOP, office
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.application.test_order_intake import Intake
from tests.execution_fakes import Approvals, Capabilities, MemoryActionStore, Principals, Shops
from tests.execution_fakes import real_catalog as catalog_of
from tests.job_fakes import MemoryJobQueue
from tests.order_intake_fakes import (
    FAKE_LINE,
    FAKE_NAME,
    FAKE_TRACKING,
    NOW,
    paid_receipt,
    shipped,
)

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = 4000000001
GRACE = timedelta(minutes=20)
_O = OrderState


@dataclass
class PagedMarketplace(FakeMarketplace):
    """Etsy's receipts read page by page, with the open-orders filter (`was_paid`, `was_shipped`)
    and the `min_last_modified` window."""

    @override
    async def list_order_receipts(self, shop_id: int, **filters: Any) -> Page[OrderReceipt]:
        self.calls.append(("list_order_receipts", shop_id, filters))
        if self.fail is not None:
            raise self.fail
        found = list(self.order_receipts)
        since = filters.get("min_last_modified")
        if since is not None:
            found = [r for r in found if r.updated_at >= since]
        if filters.get("was_paid") is True:
            found = [r for r in found if r.is_paid]
        if filters.get("was_shipped") is False:
            found = [r for r in found if not r.is_shipped]
        offset, limit = filters.get("offset", 0), filters.get("limit", 100)
        return Page(
            items=tuple(found[offset : offset + limit]), total_count=len(found), offset=offset
        )


def _many(count: int, *, days_ago: int = 1) -> tuple[OrderReceipt, ...]:
    return tuple(
        replace(
            paid_receipt(RECEIPT + n),
            created_at=NOW - timedelta(days=days_ago),
            updated_at=NOW - timedelta(days=days_ago, minutes=n),
        )
        for n in range(count)
    )


def _intake(receipts: tuple[OrderReceipt, ...]) -> Intake:
    return Intake(PagedMarketplace(order_receipts=receipts))


# --- Drifts (AC-5.5.4) --------------------------------------------------------------------------


@pytest.mark.ac("AC-5.5.4")
def test_drifts__status_items_and_shipments_with_both_values() -> None:
    stored_receipt = paid_receipt(RECEIPT)
    stored = StoredOrder(stored_receipt.updated_at, _O.AWAITING_SHIPMENT, b"x" * 32,
                         observed_facts(stored_receipt))  # fmt: skip
    etsy = shipped(replace(stored_receipt, lines=stored_receipt.lines[:1]), minutes=5)

    drifts = {d.kind: d for d in detect_drifts(stored, etsy, now=NOW, grace=GRACE)}

    assert set(drifts) == {DriftKind.STATUS, DriftKind.ITEMS, DriftKind.SHIPMENTS}
    assert drifts[DriftKind.STATUS].stored == {"status": "paid", "is_shipped": False}
    assert drifts[DriftKind.STATUS].observed == {"status": "completed", "is_shipped": True}
    assert drifts[DriftKind.ITEMS].stored == [[RECEIPT * 10, 2], [RECEIPT * 10 + 1, 1]]
    assert drifts[DriftKind.ITEMS].observed == [[RECEIPT * 10, 2]]
    assert drifts[DriftKind.SHIPMENTS].stored == []


@pytest.mark.ac("AC-5.5.4")
def test_drifts__a_matching_order_has_none() -> None:
    receipt = paid_receipt(RECEIPT)
    stored = StoredOrder(receipt.updated_at, _O.AWAITING_SHIPMENT, None, observed_facts(receipt))
    assert detect_drifts(stored, receipt, now=NOW, grace=GRACE) == ()


@pytest.mark.ac("AC-5.5.4")
def test_drifts__missing_only_once_older_than_the_grace() -> None:
    young = replace(paid_receipt(RECEIPT), updated_at=NOW - timedelta(minutes=5))
    old = replace(young, updated_at=NOW - timedelta(hours=1))
    assert detect_drifts(None, young, now=NOW, grace=GRACE) == ()
    [missing] = detect_drifts(None, old, now=NOW, grace=GRACE)
    assert missing.kind is DriftKind.MISSING
    assert missing.stored is None


@pytest.mark.ac("AC-5.5.4")
def test_drift_event__orders_drift_detected_with_no_buyer_data() -> None:
    receipt = shipped(paid_receipt(RECEIPT), minutes=5)
    old = replace(receipt, updated_at=NOW - timedelta(hours=2))
    context = IngestContext(IngestSource.RECONCILIATION, detect_drift=True)

    plan = plan_ingest(old, None, context, organization_id=ORG, shop_id=SHOP, now=NOW)

    [drift_event] = [e for e in plan.events if e.payload["event_type"] == DRIFT_DETECTED]
    assert drift_event.payload["kind"] == "missing"
    text = json.dumps([e.payload for e in plan.events])
    for secret in (FAKE_NAME, FAKE_LINE, FAKE_TRACKING):
        assert secret not in text


# --- Reconciliation runs ------------------------------------------------------------------------


@pytest.mark.ac("AC-5.5.1")
async def test_reconcile__200_receipts_ingested_by_reconciliation_alone() -> None:
    receipts = _many(200)
    intake = _intake(receipts)

    outcome = await intake.handler(intake.context({"mode": RECONCILE, "reason": RECONCILE}))

    assert len(intake.store.orders) == 200
    reads = [c for c in intake.marketplace.calls if c[0] == "list_order_receipts"]
    assert [c[2]["offset"] for c in reads if c[2].get("was_paid") is None] == [0, 100]
    assert outcome.output["receipts_read"] == 400  # type: ignore[union-attr]  # 30 days and open
    assert {source for _, _, source in intake.store.transitions} == {"reconciliation"}
    [(shop, kind, started, error)] = intake.store.cursors
    assert (shop, kind, started, error) == (SHOP, RECONCILE, NOW, None)


@pytest.mark.ac("AC-5.5.2")
async def test_reconcile__a_receipt_whose_webhook_was_lost__in_the_next_interval() -> None:
    known = _many(3)
    intake = _intake(known)
    await intake.handler(intake.context({"mode": RECONCILE}))
    lost = replace(paid_receipt(RECEIPT + 99), updated_at=NOW - timedelta(minutes=9))
    intake.marketplace.order_receipts = (*known, lost)

    # The next run: at most one reconciliation interval later.
    queue = MemoryJobQueue()

    @dataclass
    class Directory:
        def connected_shops(self) -> list[TenantShop]:
            return [TenantShop(ORG, SHOP)]

    assert enqueue_reconciliations(Directory(), queue, RECONCILE_SCHEDULE, now=NOW) == 1
    await intake.handler(intake.context({"mode": RECONCILE}))

    assert (SHOP, RECEIPT + 99) in intake.store.orders
    assert RECONCILE_SCHEDULE.every == timedelta(minutes=10)


async def test_reconcile__paid_unshipped_orders_of_any_age_are_read() -> None:
    old_open = replace(paid_receipt(RECEIPT), created_at=NOW - timedelta(days=200),
                       updated_at=NOW - timedelta(days=199))  # fmt: skip
    intake = _intake((old_open,))

    await intake.handler(intake.context({"mode": RECONCILE}))

    assert (SHOP, RECEIPT) in intake.store.orders
    opened = [c for c in intake.marketplace.calls if c[2].get("was_paid") is True]
    assert opened
    assert opened[0][2]["was_shipped"] is False
    assert opened[0][2].get("min_last_modified") is None  # no period limit


@pytest.mark.ac("AC-5.5.4")
async def test_reconcile__a_drift_is_recorded_once_with_its_event() -> None:
    receipt = replace(paid_receipt(RECEIPT), updated_at=NOW - timedelta(hours=3))
    intake = _intake((receipt,))
    intake.store.now = NOW

    await intake.handler(intake.context({"mode": RECONCILE}))
    await intake.handler(intake.context({"mode": RECONCILE}))

    kinds = [d.kind for _, d in intake.store.drifts]
    assert kinds == [DriftKind.MISSING]  # the second run finds it stored
    drift_events = [
        e for e in intake.store.events.values() if e.payload["event_type"] == DRIFT_DETECTED
    ]
    assert len(drift_events) == 1


async def test_sweep__also_compares_etsys_count_with_the_store() -> None:
    intake = _intake(_many(5))

    outcome = await intake.handler(intake.context({"mode": SWEEP}))

    assert outcome.output["etsy_receipts"] == 5  # type: ignore[union-attr]
    assert outcome.output["orders_stored"] == 5  # type: ignore[union-attr]
    assert intake.store.cursors[-1][1] == SWEEP


async def test_reconcile__a_lock_held_by_another_run__skipped() -> None:
    intake = _intake(_many(1))
    intake.store.held.add(SHOP)

    outcome = await intake.handler(intake.context({"mode": RECONCILE}))

    assert outcome.output == {"mode": RECONCILE, "skipped": "locked"}  # type: ignore[union-attr]
    assert intake.store.cursors == []


async def test_reconcile__a_failure_records_its_type_on_the_cursor() -> None:
    intake = _intake(_many(1))
    intake.marketplace.fail = ConnectionError("FAKE-DETAIL-NEVER-STORED")

    with pytest.raises(Exception):  # noqa: B017, PT011 - the run fails into the engine's retry
        await intake.handler(intake.context({"mode": RECONCILE}))

    [(_, _, _, error)] = intake.store.cursors
    assert error is not None
    assert "FAKE-DETAIL" not in error


# --- The schedule and the budget (AC-5.5.3) -----------------------------------------------------


def test_fan_out__one_job_per_shop_and_period_under_the_orders_lock() -> None:
    queue = MemoryJobQueue()

    @dataclass
    class Directory:
        shops: list[TenantShop] = field(
            default_factory=lambda: [TenantShop(ORG, SHOP), TenantShop(ORG, uuid.uuid4())]
        )

        def connected_shops(self) -> list[TenantShop]:
            return self.shops

    directory = Directory()
    for worker in range(2):  # two workers in one period
        enqueue_reconciliations(
            directory, queue, RECONCILE_SCHEDULE, now=NOW + timedelta(seconds=worker)
        )
    enqueue_reconciliations(directory, queue, SWEEP_SCHEDULE, now=NOW)

    jobs = list(queue.jobs.values())
    assert len(jobs) == 4
    assert {j.spec.kind for j in jobs} == {ORDER_SYNC_JOB}
    assert {j.spec.payload["reason"] for j in jobs} == {RECONCILE, SWEEP}
    assert all(j.spec.lock_key == f"shop:{j.spec.shop_id}:orders" for j in jobs)


def _etsy_budget() -> tuple[int, int]:
    document = tomllib.loads((ROOT / "registry" / "actions" / "etsy_open_api.toml").read_text())
    [limit] = [r for r in document["rate_limits"] if r["key"] == "etsy:app"]
    return int(limit["per_second"]), int(limit["per_day"])


@pytest.mark.ac("AC-5.5.3")
def test_budget__the_pilots_daily_calls_stay_inside_the_app_budget() -> None:
    """From the schedule: the pilot shop with up to 300 orders changed in 30 days (3 pages) and
    up to 100 open orders (1 page) stays inside the placeholder 5,000 calls a day, with room for
    the dashboard, the probes and webhooks' re-reads."""
    _, per_day = _etsy_budget()
    calls = daily_etsy_calls(shops=1, pages_30_days=3, open_pages=1)
    assert calls == 144 * 4 + 5
    assert calls <= per_day // 2
    # The budget shows where it ends: shops of that size the app can reconcile.
    assert daily_etsy_calls(shops=8, pages_30_days=3, open_pages=1) <= per_day
    assert daily_etsy_calls(shops=9, pages_30_days=3, open_pages=1) > per_day


@pytest.mark.ac("AC-5.5.3")
async def test_budget__the_gateways_limiter_enforces_it() -> None:
    """Every reconciliation read passes the 5.15 limiter: past the day's budget a read is
    DEFERRED, and Etsy is not called."""
    marketplace = PagedMarketplace(order_receipts=_many(1))
    registry = office()
    catalog = catalog_of(registry)
    tight = replace(catalog.rate_budgets["etsy:app"], per_second=5, per_day=5)
    catalog = replace(catalog, rate_budgets={"etsy:app": tight})
    gateway = ActionGatewayService(
        GatewayParts(
            catalog=catalog, registry=lambda: registry, store=MemoryActionStore(),
            principals=Principals({}), shops=Shops({}), approvals=Approvals(),
            adapters={Channel.API: EtsyApiChannel(marketplace, EtsyShops())},
            capabilities=Capabilities({(SHOP, "receipts_read")}), clock=lambda: NOW,
            guards=Guards(MemoryBreakers(), MemoryRateLimiter(), MemorySlots()),
        )
    )  # fmt: skip
    intake = Intake(marketplace)
    outcomes = []
    for n in range(7):
        ctx = intake.context({"mode": RECONCILE}, gateway=gateway, attempt=n)
        result = await gateway.submit(_read_request(ctx.execution_id))
        outcomes.append(result.outcome)

    assert outcomes[:5] == [ActionOutcome.SUCCEEDED] * 5
    assert outcomes[5:] == [ActionOutcome.DEFERRED] * 2
    assert len(marketplace.calls) == 5


def _read_request(execution_id: uuid.UUID) -> ActionRequest:
    return ActionRequest(
        action=RECEIPTS_READ,
        actor=ActorRef(ActorKind.AUTOMATION, "etsy-order-intake"),
        organization_id=ORG,
        shop_id=SHOP,
        idempotency_key=f"{execution_id}:read",
        correlation_id="c",
        input={"order_history": True, "limit": 100, "offset": 0},
    )
