"""Phase 6 stores in memory, with the same rules as Postgres: one tracking event per parcel, one
open incident per key, the incident machine on every step, events once per key."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from htco_application.incidents import IncidentRecord, IncidentStep, StaleIncident
from htco_application.messaging import OutboxMessage
from htco_application.sla_inspector import AwaitingOrder
from htco_application.tracking_sync import RecordedTracking, TrackingObservation
from htco_domain.fulfillment import SlaPolicy
from htco_domain.incidents import IncidentState, check_incident_transition


@dataclass
class Events:
    """The outbox: one message per idempotency key."""

    messages: dict[str, OutboxMessage] = field(default_factory=dict)

    def add(self, message: OutboxMessage) -> None:
        self.messages.setdefault(message.idempotency_key, message)

    def of(self, event_type: str) -> list[OutboxMessage]:
        return [m for m in self.messages.values() if m.payload.get("event_type") == event_type]


@dataclass
class MemoryTrackingStore:
    events: Events = field(default_factory=Events)
    rows: dict[tuple[uuid.UUID, int, bytes], TrackingObservation] = field(default_factory=dict)
    shipped: dict[uuid.UUID, list[int]] = field(default_factory=dict)
    # Receipts read since their last change on Etsy (`tracking_checks`).
    checked: set[tuple[uuid.UUID, int]] = field(default_factory=set)

    def record_tracking(
        self,
        observation: TrackingObservation,
        event: OutboxMessage,
        *,
        now: datetime,  # noqa: ARG002 - the port
    ) -> RecordedTracking:
        key = (observation.shop_id, observation.receipt_id, observation.fingerprint)
        first = self.rows.get(key)
        if first is not None:
            return RecordedTracking(first.id, False, first.observed_at, first.source)
        self.rows[key] = observation
        self.events.add(event)
        return RecordedTracking(observation.id, True, observation.observed_at, observation.source)

    def untracked_shipped(self, shop_id: uuid.UUID, *, limit: int) -> tuple[int, ...]:
        due = (r for r in self.shipped.get(shop_id, []) if (shop_id, r) not in self.checked)
        return tuple(due)[:limit]

    def mark_checked(self, shop_id: uuid.UUID, receipt_id: int, *, now: datetime) -> None:  # noqa: ARG002 - the port
        self.checked.add((shop_id, receipt_id))

    def changed_on_etsy(self, shop_id: uuid.UUID, receipt_id: int) -> None:
        self.checked.discard((shop_id, receipt_id))


@dataclass
class MemorySlaStore:
    tracking: MemoryTrackingStore
    awaiting: dict[uuid.UUID, list[AwaitingOrder]] = field(default_factory=dict)
    policy_rows: list[SlaPolicy] = field(default_factory=list)
    closed: dict[uuid.UUID, dict[int, datetime]] = field(default_factory=dict)
    # shop -> transaction -> receipt: the stored order items (low-review alerts, 6.9).
    transactions: dict[uuid.UUID, dict[int, int]] = field(default_factory=dict)

    def policies(self, organization_id: uuid.UUID) -> tuple[SlaPolicy, ...]:  # noqa: ARG002 - one organization
        return tuple(self.policy_rows)

    def awaiting_tracking(self, shop_id: uuid.UUID) -> tuple[AwaitingOrder, ...]:
        tracked = {r for (s, r, _) in self.tracking.rows if s == shop_id}
        closed = self.closed.get(shop_id, {})
        return tuple(
            o
            for o in self.awaiting.get(shop_id, [])
            if o.receipt_id not in tracked and o.receipt_id not in closed
        )

    def closed_orders(
        self, shop_id: uuid.UUID, receipt_ids: tuple[int, ...]
    ) -> Mapping[int, datetime]:
        closed = self.closed.get(shop_id, {})
        return {r: closed[r] for r in receipt_ids if r in closed}

    def receipts_for_transactions(
        self, shop_id: uuid.UUID, transaction_ids: tuple[int, ...]
    ) -> Mapping[int, int]:
        known = self.transactions.get(shop_id, {})
        return {t: known[t] for t in transaction_ids if t in known}


@dataclass
class MemoryIncidentStore:
    events: Events = field(default_factory=Events)
    incidents: dict[uuid.UUID, IncidentRecord] = field(default_factory=dict)
    steps: list[IncidentStep] = field(default_factory=list)

    def _open(self, organization_id: uuid.UUID, key: str) -> IncidentRecord | None:
        return next(
            (
                i
                for i in self.incidents.values()
                if i.organization_id == organization_id and i.dedup_key == key and i.state.is_open
            ),
            None,
        )

    def open_incident(
        self,
        incident: IncidentRecord,
        step: IncidentStep,
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,  # noqa: ARG002 - the port
    ) -> tuple[IncidentRecord, bool]:
        existing = self._open(incident.organization_id, incident.dedup_key)
        if existing is not None:
            return existing, False
        self.incidents[incident.id] = incident
        self.steps.append(step)
        for event in events:
            self.events.add(event)
        return incident, True

    def open_incident_for(self, organization_id: uuid.UUID, key: str) -> IncidentRecord | None:
        return self._open(organization_id, key)

    def get_incident(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentRecord | None:
        found = self.incidents.get(incident_id)
        return found if found is not None and found.organization_id == organization_id else None

    def move_incident(
        self,
        incident: IncidentRecord,
        steps: tuple[IncidentStep, ...],
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,
    ) -> IncidentRecord:
        current = self.incidents[incident.id]
        if current.state is not incident.state:
            raise StaleIncident("the incident moved meanwhile")
        state = current.state
        for step in steps:
            assert step.from_state is state
            check_incident_transition(state, step.to_state)
            if step.to_state is IncidentState.RESOLVED:
                assert step.evidence is not None
            state = step.to_state
        self.steps.extend(steps)
        for event in events:
            self.events.add(event)
        moved = replace(
            current,
            state=state,
            updated_at=now,
            resolved_at=now if state is IncidentState.RESOLVED else None,
        )
        self.incidents[incident.id] = moved
        return moved

    def open_incidents(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID | None
    ) -> tuple[IncidentRecord, ...]:
        return tuple(
            i
            for i in self.incidents.values()
            if i.organization_id == organization_id
            and i.state.is_open
            and (shop_id is None or i.shop_id == shop_id)
        )
