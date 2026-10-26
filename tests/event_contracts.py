"""Check a built event against its JSON Schema in `packages/contracts/events/` (AUD-01 AC-3)."""

import json
import uuid
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from htco_application.execution.schema import validate
from htco_application.kernel import EVENTS_DESTINATION, JsonValue, OutboxMessage

EVENTS_DIR = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "events"


@cache
def load_event_schema(event_type: str) -> dict[str, Any]:
    path = EVENTS_DIR / f"{event_type}.json"
    assert path.is_file(), f"no schema for {event_type}: add {path.name}"
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def assert_event_contract(event: OutboxMessage | Mapping[str, JsonValue]) -> None:
    """The envelope matches its event type's schema (missing, extra or mistyped fields fail)."""
    envelope = event.payload if isinstance(event, OutboxMessage) else event
    event_type = envelope.get("event_type")
    assert isinstance(event_type, str), "the event names no type"
    problems = validate(dict(envelope), load_event_schema(event_type))
    assert problems == [], f"{event_type} breaks its contract: {problems}"


def check_if_event(message: OutboxMessage) -> OutboxMessage:
    """For the test outboxes: an enveloped `events.publish` message must match its schema.
    Flat messages (the gateway's `actions.*` until its follow-up) pass through unchecked."""
    if message.destination == EVENTS_DESTINATION and "payload_version" in message.payload:
        assert_event_contract(message)
    return message


def sample_envelope(event_type: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "organization_id": str(uuid.uuid4()),
        "shop_id": str(uuid.uuid4()),
        "occurred_at": "2026-10-26T18:00:00+00:00",
        "correlation_id": None,
        "causation_id": None,
        "idempotency_key": f"{event_type}:1",
        "actor": {"type": "system", "id": "test"},
        "payload_version": 1,
        "payload": payload,
    }
