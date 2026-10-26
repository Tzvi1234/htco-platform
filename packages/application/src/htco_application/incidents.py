"""Incidents (slice 6.3): opening with deduplication, moving along the machine, and resolving on
evidence only (FR-INC-02, FR-INC-03, FR-INC-05).

An incident is opened by a detection (an SLA breach, a reconciliation drift, a mismatch of the
tracking relay, a dead-lettered automation). The store keeps one open incident per
deduplication key, `{cause}:{resource_kind}:{resource_id}`, even under concurrent openings: a
second detection returns the open incident and writes nothing (AC-6.3.1). Opening writes the
incident, its first event and the `incidents.opened.v1` event (and, for a breach, the
`fulfillment.sla_breached.v1` event) in one transaction; the notification dispatcher of slice 6.4
consumes `incidents.opened.v1`.

Evidence (a tracking event recorded from Etsy's receipt, an order cancelled, a drift cleared) moves
an open incident through `VERIFYING` to `RESOLVED`, recording the evidence with each step and the
`incidents.resolved.v1` event (AC-6.3.2). No call resolves an incident without evidence.

Nothing here holds buyer data: an incident names its order by shop and receipt number only.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from htco_application.kernel import EVENTS_DESTINATION, JsonValue, Offload, run_inline
from htco_application.messaging import OutboxMessage
from htco_domain.actors import ActorRef
from htco_domain.incidents import (
    DEFAULT_SEVERITY,
    Cause,
    Evidence,
    IncidentState,
    ResourceKind,
    Severity,
    check_incident_transition,
    dedup_key,
    resolution_path,
)

INCIDENT_OPENED = "incidents.opened.v1"
INCIDENT_RESOLVED = "incidents.resolved.v1"
# An incident moved to escalated (by the inspector's age rule or a person): the owner is told
# once per incident (slice 6.7).
INCIDENT_ESCALATED = "incidents.escalated.v1"
SLA_BREACHED = "fulfillment.sla_breached.v1"


def order_resource(shop_id: uuid.UUID, receipt_id: int) -> str:
    return f"{shop_id}/{receipt_id}"


@dataclass(frozen=True)
class Detection:
    """What a detector found. `details` are identifiers and figures, never buyer data."""

    organization_id: uuid.UUID
    cause: Cause
    resource_kind: ResourceKind
    resource_id: str
    detected_at: datetime
    source: str  # the detector: "sla_inspector", "reconciliation", "tracking_relay", ...
    shop_id: uuid.UUID | None = None
    receipt_id: int | None = None
    severity: Severity | None = None
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    correlation_id: str | None = None
    # Extra events written with the opening, in the same transaction (a breach event).
    events: tuple[OutboxMessage, ...] = ()

    @property
    def key(self) -> str:
        return dedup_key(self.cause, self.resource_kind, self.resource_id)


@dataclass(frozen=True)
class IncidentRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    dedup_key: str
    cause: Cause
    resource_kind: ResourceKind
    resource_id: str
    severity: Severity
    state: IncidentState
    detected_at: datetime
    updated_at: datetime
    source: str
    shop_id: uuid.UUID | None = None
    receipt_id: int | None = None
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class IncidentStep:
    """One recorded edge of an incident (`incident_events`)."""

    incident_id: uuid.UUID
    organization_id: uuid.UUID
    from_state: IncidentState | None
    to_state: IncidentState
    actor: ActorRef
    at: datetime
    evidence: Evidence | None = None
    note: str | None = None  # a reason code, never free text from a buyer
    correlation_id: str | None = None


class StaleIncident(Exception):  # noqa: N818 - a domain-style error named for what happened
    """The incident moved meanwhile: the caller re-reads and decides again."""


class IncidentStore(Protocol):
    """Synchronous (psycopg's pool); run through `offload`."""

    def open_incident(
        self,
        incident: IncidentRecord,
        step: IncidentStep,
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,
    ) -> tuple[IncidentRecord, bool]:
        """Insert unless an open incident has the key; returns (the open incident, created).
        `step` and `events` are written only when created."""
        ...

    def open_incident_for(self, organization_id: uuid.UUID, key: str) -> IncidentRecord | None: ...

    def get_incident(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentRecord | None: ...

    def move_incident(
        self,
        incident: IncidentRecord,
        steps: tuple[IncidentStep, ...],
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,
    ) -> IncidentRecord:
        """Apply the steps if the incident is still in `incident.state`; else `StaleIncident`."""
        ...

    def open_incidents(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID | None
    ) -> tuple[IncidentRecord, ...]: ...


def _event(
    name: str,
    incident: IncidentRecord,
    *,
    extra: Mapping[str, JsonValue],
    correlation: str | None,
    occurrence: str | None = None,
) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "event_type": name,
        "incident_id": str(incident.id),
        "dedup_key": incident.dedup_key,
        "cause": incident.cause.value,
        "severity": incident.severity.value,
        "resource_kind": incident.resource_kind.value,
        "resource_id": incident.resource_id,
        "receipt_id": incident.receipt_id,
        **extra,
    }
    return OutboxMessage(
        organization_id=incident.organization_id,
        shop_id=incident.shop_id,
        destination=EVENTS_DESTINATION,
        # An event that may happen again for the incident (an escalation) is keyed by its ID.
        idempotency_key=f"{name}:{incident.id}" + ("" if occurrence is None else f":{occurrence}"),
        payload=payload,
        correlation_id=None if correlation is None else correlation[:200],
    )


@dataclass
class IncidentService:
    store: IncidentStore
    clock: Callable[[], datetime]
    new_id: Callable[[], uuid.UUID] = uuid.uuid7
    offload: Offload = run_inline

    async def open(self, detection: Detection, *, actor: ActorRef) -> tuple[IncidentRecord, bool]:
        """One open incident per cause and resource (AC-6.3.1): a repeat returns the first."""
        now = self.clock()
        severity = detection.severity or DEFAULT_SEVERITY[detection.cause]
        incident = IncidentRecord(
            id=self.new_id(),
            organization_id=detection.organization_id,
            dedup_key=detection.key,
            cause=detection.cause,
            resource_kind=detection.resource_kind,
            resource_id=detection.resource_id,
            severity=severity,
            state=IncidentState.DETECTED,
            detected_at=detection.detected_at,
            updated_at=now,
            source=detection.source,
            shop_id=detection.shop_id,
            receipt_id=detection.receipt_id,
            details=dict(detection.details),
        )
        step = IncidentStep(
            incident_id=incident.id,
            organization_id=incident.organization_id,
            from_state=None,
            to_state=IncidentState.DETECTED,
            actor=actor,
            at=now,
            note=detection.source,
            correlation_id=detection.correlation_id,
        )
        opened = _event(
            INCIDENT_OPENED,
            incident,
            extra={"detected_at": detection.detected_at.isoformat(), "source": detection.source},
            correlation=detection.correlation_id,
        )
        events = (opened, *detection.events)
        opened_incident: tuple[IncidentRecord, bool] = await self.offload(
            lambda: self.store.open_incident(incident, step, events, now=now)
        )
        return opened_incident

    async def resolve_on_evidence(
        self,
        organization_id: uuid.UUID,
        key: str,
        evidence: Evidence | None,
        *,
        actor: ActorRef,
        correlation_id: str | None = None,
    ) -> IncidentRecord | None:
        """Resolve the open incident with this key on `evidence` (AC-6.3.2); None when no
        incident is open. Without evidence `ResolutionRefused` is raised and nothing moves."""
        incident = await self.offload(lambda: self.store.open_incident_for(organization_id, key))
        if incident is None:
            return None
        path = resolution_path(incident.state, evidence)
        return await self._apply(
            incident, path, actor=actor, evidence=evidence, note=None, correlation_id=correlation_id
        )

    async def move(  # noqa: PLR0913 - one step's facts
        self,
        organization_id: uuid.UUID,
        incident_id: uuid.UUID,
        target: IncidentState,
        *,
        actor: ActorRef,
        note: str | None = None,
        correlation_id: str | None = None,
    ) -> IncidentRecord | None:
        """A person's or a rule's step along the machine (AC-6.3.3: an illegal one raises).
        `RESOLVED` is refused here: resolution needs evidence (`resolve_on_evidence`)."""
        incident = await self.offload(lambda: self.store.get_incident(organization_id, incident_id))
        if incident is None:
            return None
        if target is IncidentState.RESOLVED:
            resolution_path(incident.state, None)  # raises ResolutionRefused
        check_incident_transition(incident.state, target)
        return await self._apply(
            incident,
            (target,),
            actor=actor,
            evidence=None,
            note=note,
            correlation_id=correlation_id,
        )

    async def _apply(  # noqa: PLR0913 - one transition's facts
        self,
        incident: IncidentRecord,
        path: tuple[IncidentState, ...],
        *,
        actor: ActorRef,
        evidence: Evidence | None,
        note: str | None,
        correlation_id: str | None,
    ) -> IncidentRecord:
        now = self.clock()
        steps: list[IncidentStep] = []
        current = incident.state
        for target in path:
            check_incident_transition(current, target)
            steps.append(
                IncidentStep(
                    incident_id=incident.id,
                    organization_id=incident.organization_id,
                    from_state=current,
                    to_state=target,
                    actor=actor,
                    at=now,
                    evidence=evidence,
                    note=note,
                    correlation_id=correlation_id,
                )
            )
            current = target
        events: tuple[OutboxMessage, ...] = ()
        if current is IncidentState.ESCALATED and incident.state is not IncidentState.ESCALATED:
            # Each escalation has its own ID (Codex, #163): two in one second are still two, and
            # the event's key and the notification's dedup key both name it.
            escalation_id = str(self.new_id())
            events = (
                _event(
                    INCIDENT_ESCALATED,
                    incident,
                    extra={
                        "escalated_from": incident.state.value,
                        "escalated_at": now.isoformat(),
                        "escalation_id": escalation_id,
                    },
                    correlation=correlation_id,
                    occurrence=escalation_id,
                ),
            )
        if current is IncidentState.RESOLVED and evidence is not None:
            events = (
                _event(
                    INCIDENT_RESOLVED,
                    incident,
                    extra={
                        "evidence_kind": evidence.kind.value,
                        "evidence_source": evidence.source,
                        "evidence_reference": evidence.reference,
                        "resolved_at": now.isoformat(),
                    },
                    correlation=correlation_id,
                ),
            )
        frozen = tuple(steps)
        moved: IncidentRecord = await self.offload(
            lambda: self.store.move_incident(incident, frozen, events, now=now)
        )
        return moved

    async def open_incidents(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID | None = None
    ) -> tuple[IncidentRecord, ...]:
        found: tuple[IncidentRecord, ...] = await self.offload(
            lambda: self.store.open_incidents(organization_id, shop_id)
        )
        return found


# --- Drifts from reconciliation (AC-6.3.4) ------------------------------------------------------


@dataclass(frozen=True)
class ObservedDrift:
    """A drift that reconciliation (slice 5.5, `app.sync_drifts`) recorded: identifiers only."""

    organization_id: uuid.UUID
    shop_id: uuid.UUID
    receipt_id: int
    kind: str  # "missing", "status", "items", "shipments"
    detected_at: datetime
    drift_id: uuid.UUID
    correlation_id: str | None = None


def drift_detection(drift: ObservedDrift) -> Detection:
    """One incident per order and cause, whatever kind of drift the order shows; the drift
    itself is linked by its ID."""
    return Detection(
        organization_id=drift.organization_id,
        cause=Cause.SYNC_DRIFT,
        resource_kind=ResourceKind.ORDER,
        resource_id=order_resource(drift.shop_id, drift.receipt_id),
        detected_at=drift.detected_at,
        source="reconciliation",
        shop_id=drift.shop_id,
        receipt_id=drift.receipt_id,
        details={"drift_id": str(drift.drift_id), "drift_kind": drift.kind},
        correlation_id=drift.correlation_id,
    )
