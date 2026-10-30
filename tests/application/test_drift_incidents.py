"""The drift trigger (AC-6.3.4, slice 6.7): every `orders.drift_detected.v1` starts one Incident
Detection & Escalation run for its drift, which opens one `sync_drift` incident per order.

The drift's ID is its row's natural key (shop, receipt, kind, Etsy update time), so the event
names the very `app.sync_drifts` row it recorded, whichever path found it: reconciliation or
Order Deduplication & Verification.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from htco_application.automation.durable import Completed
from htco_application.execution.schema import validate
from htco_application.jobs import JobError
from htco_application.kernel import event_payload
from htco_application.messaging import OutboxMessage
from htco_application.order_automations import Difference, difference_event
from htco_application.order_ingest import (
    DRIFT_DETECTED,
    IngestContext,
    IngestSource,
    drift_id,
    plan_ingest,
)
from htco_application.sla_inspector import DriftIncidents, StoredDrift
from htco_domain.automation import TriggerKind
from htco_domain.incidents import Cause, IncidentState
from tests.application.test_sla_inspector import Inspector
from tests.application.test_tracking_sync import Engine
from tests.event_contracts import assert_event_contract
from tests.log_capture import capture_logs
from tests.order_intake_fakes import paid_receipt

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000d001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000d002")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
RECEIPT = 4000000001
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "packages/contracts/automations/incident-detection-escalation.input.json"
)
OUTPUT_SCHEMA = SCHEMA.with_name("incident-detection-escalation.output.json")


def drift_message(**payload: Any) -> OutboxMessage:
    values: dict[str, Any] = {
        "event_type": DRIFT_DETECTED,
        "shop_id": str(SHOP),
        "receipt_id": RECEIPT,
        "kind": "status",
        "stored": {"status": "paid"},
        "observed": {"status": "completed"},
        "etsy_updated_at": (NOW - timedelta(hours=1)).isoformat(),
        "drift_id": str(uuid.UUID(int=9)),
        "detected_at": NOW.isoformat(),
    } | payload
    return OutboxMessage(
        organization_id=ORG,
        shop_id=SHOP,
        destination="events.publish",
        idempotency_key=f"{DRIFT_DETECTED}:{SHOP}:{RECEIPT}:status:1",
        payload=values,
        correlation_id="corr-drift",
    )


# --- The drift's ID ---------------------------------------------------------------------------


def test_a_drifts_id_is_its_rows_natural_key() -> None:
    at = NOW - timedelta(hours=1)

    assert drift_id(SHOP, RECEIPT, "status", at) == drift_id(SHOP, RECEIPT, "status", at)
    assert drift_id(SHOP, RECEIPT, "status", at) != drift_id(SHOP, RECEIPT, "items", at)
    assert drift_id(SHOP, RECEIPT, "status", at) != drift_id(SHOP, RECEIPT, "status", NOW)


@pytest.mark.ac("AC-6.3.4")
def test_a_reconciliation_drift_event_names_its_drift_and_when_it_was_seen() -> None:
    old = replace(paid_receipt(RECEIPT), updated_at=NOW - timedelta(hours=2))
    context = IngestContext(IngestSource.RECONCILIATION, detect_drift=True)

    plan = plan_ingest(old, None, context, organization_id=ORG, shop_id=SHOP, now=NOW)

    [event] = [e for e in plan.events if e.payload["event_type"] == DRIFT_DETECTED]
    assert_event_contract(event)
    facts = event_payload(event)
    assert facts["drift_id"] == str(drift_id(SHOP, RECEIPT, "missing", old.updated_at))
    assert facts["detected_at"] == NOW.isoformat()


@pytest.mark.ac("AC-6.3.4")
def test_a_verification_difference_event_names_its_drift_and_when_it_was_seen() -> None:
    receipt = paid_receipt(RECEIPT)
    integration = SimpleNamespace(shop_id=SHOP, organization_id=ORG)

    event = difference_event(
        integration,  # type: ignore[arg-type]  # only its two IDs are read
        receipt,
        Difference("totals", {"amount": "hidden"}, {"amount": "hidden"}),
        "corr-verify",
        detected_at=NOW,
    )

    assert_event_contract(event)
    facts = event_payload(event)
    assert facts["drift_id"] == str(drift_id(SHOP, RECEIPT, "totals", receipt.updated_at))
    assert facts["detected_at"] == NOW.isoformat()


# --- The trigger ------------------------------------------------------------------------------


@pytest.mark.ac("AC-6.3.4")
async def test_a_drift_event_starts_one_incident_detection_for_its_drift() -> None:
    engine = Engine()
    subscriber = DriftIncidents(engine)

    await subscriber.handle(drift_message())
    await subscriber.handle(drift_message())  # a redelivery: the same key, the engine keeps one

    first, second = engine.requests
    assert first == second
    assert first.automation_id == "incident-detection-escalation"
    assert first.trigger is TriggerKind.EVENT
    assert first.idempotency_key == f"incident-detection-escalation:drift:{uuid.UUID(int=9)}"
    assert (first.organization_id, first.shop_id) == (ORG, SHOP)
    assert first.correlation_id == "corr-drift"
    assert first.causation_id == f"outbox:{DRIFT_DETECTED}:{SHOP}:{RECEIPT}:status:1"
    assert first.payload == {
        "drift": {
            "shop_id": str(SHOP),
            "receipt_id": RECEIPT,
            "kind": "status",
            "detected_at": NOW.isoformat(),
            "drift_id": str(uuid.UUID(int=9)),
        }
    }
    assert "stored" not in json.dumps(first.payload)  # identifiers only, no compared values


@pytest.mark.parametrize("kind", ["missing", "status", "items", "shipments", "totals"])
def test_every_drift_kind_is_valid_input_for_the_automation(kind: str) -> None:
    """Verification records `totals` too (5.11); the automation must accept it."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    request = DriftIncidents.request_for(drift_message(kind=kind))

    assert request is not None
    assert validate(dict(request.payload), schema) == []


async def test_the_triggered_payload_opens_one_sync_drift_incident() -> None:
    engine = Engine()
    await DriftIncidents(engine).handle(drift_message())
    inspector = Inspector(NOW)

    outcome = await inspector.handler(inspector.context(dict(engine.requests[0].payload)))

    assert isinstance(outcome, Completed)
    assert outcome.output["opened"] == 1
    [incident] = inspector.incident_store.incidents.values()
    assert incident.cause is Cause.SYNC_DRIFT


async def test_a_draft_automation_is_refused_and_logged_without_failing_the_delivery() -> None:
    engine = Engine(refuse=True)

    with capture_logs() as logs:
        await DriftIncidents(engine).handle(drift_message())

    assert len(engine.requests) == 1
    assert "fulfillment.drift_not_started" in logs.raw()


def stored_drift(message: OutboxMessage) -> StoredDrift:
    """The `app.sync_drifts` row the event names."""
    facts = event_payload(message)
    return StoredDrift(
        drift_id=uuid.UUID(str(facts["drift_id"])),
        receipt_id=int(str(facts["receipt_id"])),
        kind=str(facts["kind"]),
        detected_at=datetime.fromisoformat(str(facts["detected_at"])),
    )


@pytest.mark.ac("AC-6.3.4")
async def test_a_drift_refused_while_draft__opens_exactly_one_incident_after_activation() -> None:
    """Codex post-merge review (owner gate v4): the draft automation refused the drift event and
    the outbox acknowledged it for good. The first inspection after the activation opens its
    incident from the stored drift, linked by its ID, and later runs open nothing more."""
    message = drift_message()
    draft = Engine(refuse=True)
    await DriftIncidents(draft).handle(message)  # refused and acknowledged: no run, no incident
    inspector = Inspector(NOW + timedelta(minutes=15))
    inspector.sla.drifts[SHOP] = [stored_drift(message)]
    assert inspector.incident_store.incidents == {}

    # Activated: the schedule's inspection runs.
    first = await inspector.handler(inspector.context({"mode": "inspect"}))
    again = await inspector.handler(inspector.context({"mode": "inspect"}))

    [incident] = inspector.incident_store.incidents.values()
    assert incident.cause is Cause.SYNC_DRIFT
    assert incident.receipt_id == RECEIPT
    assert incident.details["drift_id"] == str(uuid.UUID(int=9))
    assert incident.detected_at == NOW  # when the drift was seen, not when it was backfilled
    assert isinstance(first, Completed)
    assert first.output["drifts_backfilled"] == 1
    assert again == Completed({"checked": 0, "opened": 0, "resolved": 0, "escalated": 0})
    output_schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    assert validate(dict(first.output), output_schema) == []


async def test_a_drift_its_event_already_opened__is_never_backfilled() -> None:
    engine = Engine()
    message = drift_message()
    await DriftIncidents(engine).handle(message)
    inspector = Inspector(NOW)
    inspector.sla.drifts[SHOP] = [stored_drift(message)]
    await inspector.handler(inspector.context(dict(engine.requests[0].payload)))

    outcome = await inspector.handler(inspector.context({"mode": "inspect"}))

    assert isinstance(outcome, Completed)
    assert "drifts_backfilled" not in outcome.output
    assert len(inspector.incident_store.incidents) == 1


async def test_a_drift_after_its_orders_incident_resolved__opens_a_new_one() -> None:
    """An incident resolved before a later drift does not cover it: the backfill opens one."""
    inspector = Inspector(NOW + timedelta(days=2))
    earlier = stored_drift(drift_message())
    later = stored_drift(
        drift_message(
            drift_id=str(uuid.UUID(int=10)), detected_at=(NOW + timedelta(days=1)).isoformat()
        )
    )
    inspector.sla.drifts[SHOP] = [earlier]
    await inspector.handler(inspector.context({"mode": "inspect"}))
    [first] = inspector.incident_store.incidents.values()
    inspector.incident_store.incidents[first.id] = replace(
        first, state=IncidentState.RESOLVED, resolved_at=NOW + timedelta(hours=1)
    )
    inspector.sla.drifts[SHOP] = [earlier, later]

    outcome = await inspector.handler(inspector.context({"mode": "inspect"}))

    assert isinstance(outcome, Completed)
    assert outcome.output["drifts_backfilled"] == 1
    assert len(inspector.incident_store.incidents) == 2


@pytest.mark.parametrize(
    "payload",
    [{"drift_id": None}, {"detected_at": None}, {"receipt_id": "x"}],
    ids=["an event from before 6.7", "no time", "a malformed receipt"],
)
async def test_an_event_without_its_drift_starts_nothing(payload: dict[str, Any]) -> None:
    engine = Engine()

    await DriftIncidents(engine).handle(drift_message(**payload))

    assert engine.requests == []


@pytest.mark.parametrize(
    "execution_shop",
    [uuid.UUID("00000000-0000-0000-0000-00000000d0ff"), None],
    ids=["another shop", "no shop"],
)
async def test_a_drift_for_another_shop__is_refused_and_opens_nothing(
    execution_shop: uuid.UUID | None,
) -> None:
    """Quill on #129: the payload's shop must be the execution's own, and an execution that
    names no shop is refused too (the guard fails closed)."""
    engine = Engine()
    await DriftIncidents(engine).handle(drift_message())
    inspector = Inspector(NOW)
    context = replace(inspector.context(dict(engine.requests[0].payload)), shop_id=execution_shop)

    with pytest.raises(JobError, match="the execution's shop"):
        await inspector.handler(context)

    assert inspector.incident_store.incidents == {}


def test_a_drift_id_needs_a_moment_in_time() -> None:
    """Quill on #129: a naive time would depend on the host's time zone."""
    with pytest.raises(ValueError, match="time zone"):
        drift_id(SHOP, RECEIPT, "status", datetime(2026, 10, 25, 9, 0))  # noqa: DTZ001 - the case
