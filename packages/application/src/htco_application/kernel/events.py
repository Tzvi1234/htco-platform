"""Domain events (AUD-01): one envelope, one publisher, one reader.

Every event is a `DomainEvent` with the envelope of SOFTWARE_DESIGN §8.4. `publish` turns it into
the outbox message that the producer writes in its own transaction, addressed to
`events.publish`. Its JSON Schema is `packages/contracts/events/<event_type>.json`, and
EVENT_CATALOG lists it.

Consumers read a delivered message with `read_event`. A row written before the envelope existed
(a flat payload with `event_type` beside the facts) reads as version 1, so old outbox rows are
still delivered. A version the consumer does not accept is refused with a `DeadLetterError`,
which sends the message to the dead letters at once: retrying cannot make it readable.
"""

import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from htco_application.kernel.errors import DeadLetterError
from htco_application.kernel.outbox import MAX_OUTBOX_KEY_LENGTH, OutboxMessage
from htco_application.kernel.types import JsonValue
from htco_domain.actors import ActorRef

# The outbox destination that hands domain events to their subscribers.
EVENTS_DESTINATION = "events.publish"

# `<context>.<fact_in_past_tense>.v<N>` (EVENT_CATALOG rule 1).
EVENT_TYPE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*\.v[1-9][0-9]*$")
# Event IDs come from the tenant and the idempotency key, so producing the same event twice gives
# the same ID (consumers are idempotent on it, EVENT_CATALOG rule 5).
_EVENT_ID_NAMESPACE = uuid.UUID("5f3c1c3e-5a52-4b0c-9b8e-5c1f6a2b7d10")


@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    organization_id: uuid.UUID
    idempotency_key: str
    occurred_at: datetime
    actor: ActorRef
    # IDs and the minimum facts consumers need (EVENT_CATALOG rule 3).
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    shop_id: uuid.UUID | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    payload_version: int = 1

    def __post_init__(self) -> None:
        if not EVENT_TYPE.match(self.event_type):
            raise ValueError(f"an event type is <context>.<fact>.v<N>: {self.event_type!r}")
        if self.payload_version < 1:
            raise ValueError("an event's payload version starts at 1")
        if self.occurred_at.tzinfo is None:
            raise ValueError("an event's time is timezone-aware")
        if "event_type" in self.payload:
            raise ValueError("an event's type belongs to the envelope, not its payload")

    @property
    def event_id(self) -> uuid.UUID:
        return uuid.uuid5(_EVENT_ID_NAMESPACE, f"{self.organization_id}:{self.idempotency_key}")

    def envelope(self) -> dict[str, JsonValue]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "organization_id": str(self.organization_id),
            "shop_id": None if self.shop_id is None else str(self.shop_id),
            "occurred_at": self.occurred_at.isoformat(),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "actor": {"type": self.actor.kind.value, "id": self.actor.id},
            "payload_version": self.payload_version,
            "payload": dict(self.payload),
        }


def publish(event: DomainEvent, *, action_execution_id: uuid.UUID | None = None) -> OutboxMessage:
    """The outbox message that publishes `event`; the producer adds it in its transaction."""
    correlation = event.correlation_id
    return OutboxMessage(
        organization_id=event.organization_id,
        destination=EVENTS_DESTINATION,
        idempotency_key=event.idempotency_key,
        payload=event.envelope(),
        shop_id=event.shop_id,
        action_execution_id=action_execution_id,
        correlation_id=None if correlation is None else correlation[:MAX_OUTBOX_KEY_LENGTH],
    )


class MalformedEventError(DeadLetterError):
    """The delivered message is not an event: no type, or a broken envelope."""


class UnknownEventVersionError(DeadLetterError):
    """The consumer does not accept this payload version: never processed, dead-lettered."""


@dataclass(frozen=True)
class ReceivedEvent:
    event_type: str
    payload_version: int
    payload: Mapping[str, JsonValue]
    event_id: str | None  # None for a row written before the envelope


def read_event(
    message: Mapping[str, JsonValue], *, accepted: Collection[int] = (1,)
) -> ReceivedEvent:
    """The event in a delivered `events.publish` payload, in a version the consumer accepts."""
    event_type = message.get("event_type")
    if not isinstance(event_type, str) or not EVENT_TYPE.match(event_type):
        raise MalformedEventError("the message names no event type")
    if "payload_version" not in message:
        # Written before the envelope: the facts sit beside the type (version 1).
        facts = {k: v for k, v in message.items() if k != "event_type"}
        return ReceivedEvent(event_type, 1, facts, None)
    version = message.get("payload_version")
    payload = message.get("payload")
    event_id = message.get("event_id")
    if isinstance(version, bool) or not isinstance(version, int) or not isinstance(payload, dict):
        raise MalformedEventError(f"{event_type} has a broken envelope")
    if version not in accepted:
        raise UnknownEventVersionError(f"{event_type} version {version} is not accepted")
    return ReceivedEvent(
        event_type, version, payload, event_id if isinstance(event_id, str) else None
    )


def event_payload(message: OutboxMessage) -> Mapping[str, JsonValue]:
    """The facts of a delivered event, in either shape (subscribers of `EventDeliverer`)."""
    return read_event(message.payload).payload
