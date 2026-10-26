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
from htco_application.messaging import OutboxMessage
from htco_application.order_automations import Difference, difference_event
from htco_application.order_ingest import (
    DRIFT_DETECTED,
    IngestContext,
    IngestSource,
    drift_id,
    plan_ingest,
)
from htco_application.sla_inspector import DriftIncidents
from htco_domain.automation import TriggerKind
from htco_domain.incidents import Cause
from tests.application.test_sla_inspector import Inspector
from tests.application.test_tracking_sync import Engine
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
    assert event.payload["drift_id"] == str(drift_id(SHOP, RECEIPT, "missing", old.updated_at))
    assert event.payload["detected_at"] == NOW.isoformat()


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

    assert event.payload["drift_id"] == str(drift_id(SHOP, RECEIPT, "totals", receipt.updated_at))
    assert event.payload["detected_at"] == NOW.isoformat()


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


@pytest.mark.parametrize(
    "payload",
    [{"drift_id": None}, {"detected_at": None}, {"receipt_id": "x"}],
    ids=["an event from before 6.7", "no time", "a malformed receipt"],
)
async def test_an_event_without_its_drift_starts_nothing(payload: dict[str, Any]) -> None:
    engine = Engine()

    await DriftIncidents(engine).handle(drift_message(**payload))

    assert engine.requests == []
