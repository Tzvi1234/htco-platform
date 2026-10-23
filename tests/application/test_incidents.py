"""Incidents (slice 6.3): one open incident per cause and resource, the machine on every step,
resolution only on evidence, events once. In memory; Postgres in tests/integration."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.incidents import (
    INCIDENT_OPENED,
    INCIDENT_RESOLVED,
    Detection,
    IncidentService,
    ObservedDrift,
    StaleIncident,
    drift_detection,
    order_resource,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.incidents import (
    Cause,
    Evidence,
    EvidenceKind,
    IncidentState,
    ResolutionRefused,
    ResourceKind,
    Severity,
)
from htco_domain.state_machine import IllegalTransition
from tests.fulfillment_fakes import MemoryIncidentStore

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000c001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000c002")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
ACTOR = ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation")
OWNER = ActorRef(ActorKind.USER, str(uuid.UUID(int=7)))
EVIDENCE = Evidence(EvidenceKind.TRACKING_PRESENT, NOW, "tracking:etsy", "t-1")


def detection(receipt_id: int = 51, cause: Cause = Cause.TRACKING_MISSING) -> Detection:
    return Detection(
        organization_id=ORG,
        cause=cause,
        resource_kind=ResourceKind.ORDER,
        resource_id=order_resource(SHOP, receipt_id),
        detected_at=NOW,
        source="sla_inspector",
        shop_id=SHOP,
        receipt_id=receipt_id,
    )


def service(store: MemoryIncidentStore) -> IncidentService:
    return IncidentService(store, clock=lambda: NOW)


@pytest.mark.ac("AC-6.3.1")
async def test_ac_6_3_1_two_breaches_for_the_same_order_and_cause_open_one_incident() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)

    first, created = await incidents.open(detection(), actor=ACTOR)
    second, again = await incidents.open(detection(), actor=ACTOR)

    assert created
    assert not again
    assert second.id == first.id
    assert len(store.incidents) == 1
    assert len(store.events.of(INCIDENT_OPENED)) == 1
    assert first.state is IncidentState.DETECTED
    assert first.severity is Severity.MEDIUM


async def test_another_cause_or_order_is_another_incident() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)

    await incidents.open(detection(51), actor=ACTOR)
    await incidents.open(detection(52), actor=ACTOR)
    await incidents.open(detection(51, Cause.SYNC_DRIFT), actor=ACTOR)

    assert len(store.incidents) == 3


async def test_after_resolution_a_new_breach_opens_a_new_incident() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    first, _ = await incidents.open(detection(), actor=ACTOR)
    await incidents.resolve_on_evidence(ORG, first.dedup_key, EVIDENCE, actor=ACTOR)

    second, created = await incidents.open(detection(), actor=ACTOR)

    assert created
    assert second.id != first.id


@pytest.mark.ac("AC-6.3.2")
async def test_ac_6_3_2_evidence_resolves_through_verifying_with_the_evidence_recorded() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    incident, _ = await incidents.open(detection(), actor=ACTOR)

    resolved = await incidents.resolve_on_evidence(ORG, incident.dedup_key, EVIDENCE, actor=ACTOR)

    assert resolved is not None
    assert resolved.state is IncidentState.RESOLVED
    assert resolved.resolved_at == NOW
    path = [(s.from_state, s.to_state) for s in store.steps]
    assert path == [
        (None, IncidentState.DETECTED),
        (IncidentState.DETECTED, IncidentState.VERIFYING),
        (IncidentState.VERIFYING, IncidentState.RESOLVED),
    ]
    assert store.steps[-1].evidence == EVIDENCE
    [event] = store.events.of(INCIDENT_RESOLVED)
    assert event.payload["evidence_reference"] == "t-1"


@pytest.mark.ac("AC-6.3.2")
async def test_ac_6_3_2_without_evidence_it_never_resolves() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    incident, _ = await incidents.open(detection(), actor=ACTOR)

    with pytest.raises(ResolutionRefused):
        await incidents.resolve_on_evidence(ORG, incident.dedup_key, None, actor=ACTOR)
    with pytest.raises(ResolutionRefused):
        await incidents.move(ORG, incident.id, IncidentState.RESOLVED, actor=OWNER)

    assert store.incidents[incident.id].state is IncidentState.DETECTED
    assert store.events.of(INCIDENT_RESOLVED) == []


async def test_evidence_for_no_open_incident_does_nothing() -> None:
    assert (
        await service(MemoryIncidentStore()).resolve_on_evidence(
            ORG, "tracking_missing:order:x", EVIDENCE, actor=ACTOR
        )
        is None
    )


@pytest.mark.ac("AC-6.3.3")
async def test_ac_6_3_3_an_illegal_step_is_rejected_and_nothing_moves() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    incident, _ = await incidents.open(detection(), actor=ACTOR)

    with pytest.raises(IllegalTransition):
        await incidents.move(ORG, incident.id, IncidentState.APPROVAL_REQUIRED, actor=OWNER)

    assert store.incidents[incident.id].state is IncidentState.DETECTED
    assert len(store.steps) == 1


async def test_a_person_moves_an_incident_along_the_machine() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    incident, _ = await incidents.open(detection(), actor=ACTOR)

    moved = await incidents.move(
        ORG, incident.id, IncidentState.TRIAGED, actor=OWNER, note="owner_triage"
    )

    assert moved is not None
    assert moved.state is IncidentState.TRIAGED
    assert store.steps[-1].actor == OWNER
    assert await incidents.move(ORG, uuid.uuid4(), IncidentState.TRIAGED, actor=OWNER) is None


async def test_a_stale_incident_is_refused() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    incident, _ = await incidents.open(detection(), actor=ACTOR)
    await incidents.move(ORG, incident.id, IncidentState.TRIAGED, actor=OWNER)

    with pytest.raises(StaleIncident):
        store.move_incident(incident, (), (), now=NOW + timedelta(seconds=1))


@pytest.mark.ac("AC-6.3.4")
async def test_ac_6_3_4_a_drift_opens_one_incident_linked_to_the_order() -> None:
    store = MemoryIncidentStore()
    incidents = service(store)
    drift = ObservedDrift(ORG, SHOP, 77, "status", NOW, uuid.UUID(int=99), "recon:1")
    shipments = ObservedDrift(ORG, SHOP, 77, "shipments", NOW, uuid.UUID(int=100), "recon:2")

    first, created = await incidents.open(drift_detection(drift), actor=ACTOR)
    second, again = await incidents.open(drift_detection(shipments), actor=ACTOR)

    assert created
    assert not again
    assert second.id == first.id
    assert first.cause is Cause.SYNC_DRIFT
    assert (first.shop_id, first.receipt_id) == (SHOP, 77)
    assert first.details == {"drift_id": str(uuid.UUID(int=99)), "drift_kind": "status"}
