"""A dead-lettered execution reaches the owner (AC-6.7.4, slice 6.7).

When any automation gives up, the engine says so at ERROR (the Phase 1 alert rules, through
Sentry) and its state-change event names the automation; the event's subscriber opens one
`automation_dead_lettered` incident per automation, which notifies the Owners and Admins on the
one notification path (6.4). The Admin Health screen (5.9) shows it too, and retries it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.automation.durable import StateChange
from htco_application.incidents import INCIDENT_OPENED, DeadLetterIncidents, IncidentService
from htco_application.messaging import OutboxMessage
from htco_application.status import EXECUTION_STATE_CHANGED, execution_state_event
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import ExecutionState
from htco_domain.incidents import Cause, ResourceKind, Severity
from tests.application.test_durable_engine import WORKER, World, paid
from tests.fulfillment_fakes import MemoryIncidentStore
from tests.log_capture import capture_logs

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000f001")
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def change(to_state: ExecutionState, *, automation_id: str | None = "order-dedup-verification",
           execution: uuid.UUID | None = None) -> StateChange:  # fmt: skip
    return StateChange(
        execution_id=execution or uuid.uuid7(),
        organization_id=ORG,
        from_state=ExecutionState.FAILED,
        to_state=to_state,
        actor=ActorRef(ActorKind.SYSTEM, "worker:1"),
        correlation_id="corr-dl",
        at=NOW,
        automation_id=automation_id,
    )


def subscriber() -> tuple[DeadLetterIncidents, MemoryIncidentStore]:
    store = MemoryIncidentStore()
    return DeadLetterIncidents(IncidentService(store, clock=lambda: NOW)), store


# --- The engine names the automation and says so at ERROR ------------------------------------


def test_the_state_change_event_names_its_automation() -> None:
    event = execution_state_event(change(ExecutionState.DEAD_LETTERED))

    assert event.payload["automation_id"] == "order-dedup-verification"
    assert event.payload["to_state"] == "dead_lettered"


@pytest.mark.ac("AC-6.7.4")
async def test_a_dead_letter__is_logged_at_error_with_its_automation() -> None:
    world = World()
    world.book.failures = 99
    execution = world.engine.trigger(paid())

    with capture_logs() as logs:
        await world.engine.execute(execution.id, worker=WORKER)
        world.now += timedelta(seconds=10)
        await world.engine.execute(execution.id, worker=WORKER)

    [line] = [line for line in logs.lines() if "automation.dead_lettered" in line["event"]]
    assert line["level"] == "error"
    assert execution.automation_id in line["event"]
    assert all(c.automation_id == execution.automation_id for c in world.store.changes)


# --- The subscriber opens one incident per automation -----------------------------------------


@pytest.mark.ac("AC-6.7.4")
async def test_a_dead_letter__opens_one_incident_for_its_automation() -> None:
    incidents, store = subscriber()

    await incidents.handle(execution_state_event(change(ExecutionState.DEAD_LETTERED)))
    await incidents.handle(execution_state_event(change(ExecutionState.DEAD_LETTERED)))

    [incident] = store.incidents.values()
    assert incident.cause is Cause.AUTOMATION_DEAD_LETTERED
    assert (incident.resource_kind, incident.resource_id) == (
        ResourceKind.AUTOMATION,
        "order-dedup-verification",
    )
    assert incident.severity is Severity.HIGH
    assert incident.shop_id is None
    assert incident.receipt_id is None
    assert len(store.events.of(INCIDENT_OPENED)) == 1  # the owner is told once


async def test_another_automations_dead_letter__is_another_incident() -> None:
    incidents, store = subscriber()

    await incidents.handle(execution_state_event(change(ExecutionState.DEAD_LETTERED)))
    await incidents.handle(
        execution_state_event(change(ExecutionState.DEAD_LETTERED, automation_id="tracking-sync"))
    )

    assert {i.resource_id for i in store.incidents.values()} == {
        "order-dedup-verification",
        "tracking-sync",
    }


@pytest.mark.parametrize(
    "event",
    [
        lambda: execution_state_event(change(ExecutionState.SUCCEEDED)),
        lambda: execution_state_event(change(ExecutionState.RETRY_SCHEDULED)),
        lambda: execution_state_event(change(ExecutionState.DEAD_LETTERED, automation_id=None)),
        lambda: OutboxMessage(ORG, "events.publish", "k", {"event_type": EXECUTION_STATE_CHANGED,
                                                          "to_state": "dead_lettered"}),
        lambda: OutboxMessage(ORG, "events.publish", "k", {"event_type": EXECUTION_STATE_CHANGED,
                                                          "to_state": "dead_lettered",
                                                          "automation_id": "a", "execution_id": "e",
                                                          "occurred_at": "yesterday"}),
    ],
    ids=["succeeded", "retry-scheduled", "no-automation-named", "malformed", "not-a-time"],
)  # fmt: skip
async def test_anything_else__opens_nothing(event: Any) -> None:
    incidents, store = subscriber()

    await incidents.handle(event())

    assert store.incidents == {}
