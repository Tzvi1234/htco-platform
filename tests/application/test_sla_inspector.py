"""Incident Detection & Escalation (slices 6.2, 6.3): SLA breaches open one incident, closures
resolve, old incidents escalate, drifts open one incident. In memory."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import UnavailableGateway
from htco_application.fulfillment_schedule import (
    FULFILLMENT_SCHEDULE,
    FULFILLMENT_SHOP_JOB,
    INCIDENT_DETECTION,
    TRACKING_SYNC,
    FulfillmentInspection,
    ScheduledShop,
    enqueue_fulfillment_inspections,
)
from htco_application.incidents import (
    INCIDENT_OPENED,
    INCIDENT_RESOLVED,
    SLA_BREACHED,
    IncidentService,
)
from htco_application.jobs import ClaimedJob, JobError
from htco_application.maintenance import TenantShop
from htco_application.sla_inspector import AwaitingOrder, IncidentDetection
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.fulfillment import SlaPolicy, WindowUnit
from htco_domain.incidents import Cause, IncidentState
from tests.application.test_tracking_sync import Engine
from tests.fulfillment_fakes import MemoryIncidentStore, MemorySlaStore, MemoryTrackingStore
from tests.job_fakes import MemoryJobQueue

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000d001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000d002")
PAID = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # a Monday
THREE_DAYS = SlaPolicy(id=uuid.UUID(int=3), window=3, unit=WindowUnit.CALENDAR_DAYS)


class Inspector:
    def __init__(self, now: datetime, policies: list[SlaPolicy] | None = None) -> None:
        self.now = now
        self.tracking = MemoryTrackingStore()
        self.sla = MemorySlaStore(self.tracking, policy_rows=policies or [THREE_DAYS])
        self.incident_store = MemoryIncidentStore()

    def await_order(self, receipt_id: int, paid_at: datetime = PAID) -> None:
        self.sla.awaiting.setdefault(SHOP, []).append(AwaitingOrder(receipt_id, paid_at))

    @property
    def handler(self) -> IncidentDetection:
        incidents = IncidentService(self.incident_store, clock=lambda: self.now)
        return IncidentDetection(self.sla, incidents, clock=lambda: self.now)

    def context(self, payload: dict[str, Any]) -> HandlerContext:
        return HandlerContext(
            execution_id=uuid.uuid7(),
            automation_id=INCIDENT_DETECTION,
            actor=ActorRef(ActorKind.SYSTEM, "scheduler"),
            organization_id=ORG,
            shop_id=SHOP,
            correlation_id="sched:1",
            causation_id=None,
            attempt=1,
            payload=payload,
            gateway=UnavailableGateway(),
        )


@pytest.mark.ac("AC-6.2.1")
async def test_ac_6_2_1_at_exactly_the_deadline_no_breach_one_microsecond_later_one() -> None:
    deadline = PAID + timedelta(days=3)
    inside = Inspector(deadline)
    inside.await_order(51)
    outcome = await inside.handler(inside.context({"mode": "inspect"}))
    assert outcome == Completed({"checked": 1, "opened": 0, "resolved": 0, "escalated": 0})
    assert inside.incident_store.incidents == {}

    after = Inspector(deadline + timedelta(microseconds=1))
    after.await_order(51)
    outcome = await after.handler(after.context({"mode": "inspect"}))
    assert outcome == Completed({"checked": 1, "opened": 1, "resolved": 0, "escalated": 0})
    [breach] = after.incident_store.events.of(SLA_BREACHED)
    assert breach.payload["deadline"] == deadline.isoformat()


@pytest.mark.ac("AC-6.2.2")
async def test_ac_6_2_2_business_day_policies_wait_over_the_weekend() -> None:
    friday = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    policy = SlaPolicy(id=uuid.UUID(int=4), window=1, unit=WindowUnit.BUSINESS_DAYS)
    sunday = Inspector(friday + timedelta(days=2), [policy])
    sunday.await_order(61, friday)
    await sunday.handler(sunday.context({"mode": "inspect"}))
    assert sunday.incident_store.incidents == {}

    tuesday = Inspector(friday + timedelta(days=4), [policy])
    tuesday.await_order(61, friday)
    await tuesday.handler(tuesday.context({"mode": "inspect"}))
    assert len(tuesday.incident_store.incidents) == 1


@pytest.mark.ac("AC-6.3.1")
async def test_ac_6_3_1_inspecting_again_opens_nothing_new() -> None:
    inspector = Inspector(PAID + timedelta(days=5))
    inspector.await_order(51)
    for _ in range(4):
        await inspector.handler(inspector.context({"mode": "inspect"}))

    [incident] = inspector.incident_store.incidents.values()
    assert incident.cause is Cause.TRACKING_MISSING
    assert incident.receipt_id == 51
    assert len(inspector.incident_store.events.of(INCIDENT_OPENED)) == 1
    assert len(inspector.incident_store.events.of(SLA_BREACHED)) == 1


async def test_a_cancelled_order_resolves_its_incident_on_that_evidence() -> None:
    inspector = Inspector(PAID + timedelta(days=5))
    inspector.await_order(51)
    await inspector.handler(inspector.context({"mode": "inspect"}))
    inspector.sla.closed[SHOP] = {51: PAID + timedelta(days=4)}

    outcome = await inspector.handler(inspector.context({"mode": "inspect"}))

    assert outcome == Completed({"checked": 0, "opened": 0, "resolved": 1, "escalated": 0})
    [incident] = inspector.incident_store.incidents.values()
    assert incident.state is IncidentState.RESOLVED
    [event] = inspector.incident_store.events.of(INCIDENT_RESOLVED)
    assert event.payload["evidence_kind"] == "order_closed"


async def test_an_old_incident_escalates_once() -> None:
    inspector = Inspector(PAID + timedelta(days=5))
    inspector.await_order(51)
    await inspector.handler(inspector.context({"mode": "inspect"}))
    inspector.now += timedelta(days=4)  # medium: escalates after three days

    first = await inspector.handler(inspector.context({"mode": "inspect"}))
    second = await inspector.handler(inspector.context({"mode": "inspect"}))

    assert isinstance(first, Completed)
    assert first.output["escalated"] == 1
    assert isinstance(second, Completed)
    assert second.output["escalated"] == 0
    [incident] = inspector.incident_store.incidents.values()
    assert incident.state is IncidentState.ESCALATED
    # 6.7: the escalation is announced once, however often the inspector runs.
    assert len(inspector.incident_store.events.of("incidents.escalated.v1")) == 1


@pytest.mark.ac("AC-6.3.4")
async def test_ac_6_3_4_a_drift_payload_opens_one_incident() -> None:
    inspector = Inspector(PAID)
    drift = {
        "shop_id": str(SHOP), "receipt_id": 77, "kind": "status",
        "detected_at": PAID.isoformat(), "drift_id": str(uuid.UUID(int=5)),
    }  # fmt: skip

    first = await inspector.handler(inspector.context({"drift": drift}))
    second = await inspector.handler(inspector.context({"drift": drift | {"kind": "items"}}))

    assert isinstance(first, Completed)
    assert first.output["opened"] == 1
    assert isinstance(second, Completed)
    assert second.output["opened"] == 0
    [incident] = inspector.incident_store.incidents.values()
    assert incident.cause is Cause.SYNC_DRIFT


async def test_bad_payloads_are_refused() -> None:
    inspector = Inspector(PAID)
    with pytest.raises(JobError, match="inspect mode or a drift"):
        await inspector.handler(inspector.context({}))
    with pytest.raises(JobError, match="names its shop"):
        await inspector.handler(replace(inspector.context({"mode": "inspect"}), shop_id=None))
    with pytest.raises(JobError, match="drift names"):
        await inspector.handler(inspector.context({"drift": {"shop_id": str(SHOP)}}))
    other = {
        "shop_id": str(uuid.uuid4()), "receipt_id": 1, "kind": "status",
        "detected_at": PAID.isoformat(), "drift_id": str(uuid.uuid4()),
    }  # fmt: skip
    with pytest.raises(JobError, match="execution's shop"):
        await inspector.handler(inspector.context({"drift": other}))


# --- The schedule ---------------------------------------------------------------------------------


class Shops:
    def connected_shops(self) -> list[ScheduledShop]:
        return [TenantShop(ORG, SHOP)]


def test_the_schedule_enqueues_one_job_per_shop_and_period() -> None:
    queue = MemoryJobQueue()
    now = PAID + timedelta(minutes=7)

    assert enqueue_fulfillment_inspections(Shops(), queue, now=now) == 1
    enqueue_fulfillment_inspections(Shops(), queue, now=now + timedelta(minutes=1))

    jobs = [j for j in queue.jobs.values() if j.spec.kind == FULFILLMENT_SHOP_JOB]
    assert len(jobs) == 1
    assert FULFILLMENT_SCHEDULE.every == timedelta(minutes=15)


def _job(payload: dict[str, Any]) -> ClaimedJob:
    from htco_application.jobs import DEFAULT_RETRY  # noqa: PLC0415

    return ClaimedJob(
        id=uuid.uuid7(), kind=FULFILLMENT_SHOP_JOB, idempotency_key="k", payload=payload,
        organization_id=ORG, shop_id=SHOP, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=PAID,
    )  # fmt: skip


def test_the_shop_job_starts_both_automations_keyed_by_period() -> None:
    engine = Engine()

    started = FulfillmentInspection(engine).run(_job({"period": "2026-10-19T12:00Z"}))

    assert started == {TRACKING_SYNC: True, INCIDENT_DETECTION: True}
    keys = {r.automation_id: (r.idempotency_key, r.payload, r.trigger) for r in engine.requests}
    assert keys[TRACKING_SYNC] == (
        f"{TRACKING_SYNC}:{SHOP}:2026-10-19T12:00Z",
        {"mode": "schedule"},
        TriggerKind.SCHEDULE,
    )
    assert keys[INCIDENT_DETECTION][1] == {"mode": "inspect"}


def test_draft_automations_are_refused_without_failing_the_job() -> None:
    started = FulfillmentInspection(Engine(refuse=True)).run(_job({"period": "p"}))
    assert started == {TRACKING_SYNC: False, INCIDENT_DETECTION: False}
    with pytest.raises(JobError, match="period"):
        FulfillmentInspection(Engine()).run(_job({}))
    with pytest.raises(JobError, match="shop and organization"):
        FulfillmentInspection(Engine()).run(replace(_job({"period": "p"}), shop_id=None))
