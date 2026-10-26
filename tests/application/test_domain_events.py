"""Domain events (AUD-01, #141): one envelope (SOFTWARE_DESIGN §8.4), one publisher, one reader."""

import uuid
from datetime import UTC, datetime

import pytest

from htco_application.kernel import (
    EVENTS_DESTINATION,
    DomainEvent,
    JsonValue,
    MalformedEventError,
    OutboxMessage,
    UnknownEventVersionError,
    publish,
    read_event,
)
from htco_domain.actors import ActorKind, ActorRef

ORG = uuid.UUID("0190a000-0000-7000-8000-0000000000aa")
SHOP = uuid.UUID("0190a000-0000-7000-8000-000000000001")
AT = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)
INTAKE = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")


def _event(**changes: object) -> DomainEvent:
    fields: dict[str, object] = {
        "event_type": "orders.ingested.v1",
        "organization_id": ORG,
        "shop_id": SHOP,
        "idempotency_key": "orders.ingested.v1:shop:1",
        "occurred_at": AT,
        "actor": INTAKE,
        "payload": {"receipt_id": 1},
        "correlation_id": "corr-1",
        "causation_id": "job:1",
    }
    fields.update(changes)
    return DomainEvent(**fields)  # type: ignore[arg-type]


@pytest.mark.ac("AUD-01.1")
def test_publish__writes_the_envelope_to_the_events_destination() -> None:
    message = publish(_event())

    assert isinstance(message, OutboxMessage)
    assert message.destination == EVENTS_DESTINATION
    assert message.organization_id == ORG
    assert message.shop_id == SHOP
    assert message.idempotency_key == "orders.ingested.v1:shop:1"
    assert message.correlation_id == "corr-1"
    assert message.payload == {
        "event_id": str(_event().event_id),
        "event_type": "orders.ingested.v1",
        "organization_id": str(ORG),
        "shop_id": str(SHOP),
        "occurred_at": "2026-10-26T18:00:00+00:00",
        "correlation_id": "corr-1",
        "causation_id": "job:1",
        "idempotency_key": "orders.ingested.v1:shop:1",
        "actor": {"type": "automation", "id": "etsy-order-intake"},
        "payload_version": 1,
        "payload": {"receipt_id": 1},
    }


@pytest.mark.ac("AUD-01.1")
def test_publish__an_action_effect__keeps_its_action_execution() -> None:
    action = uuid.uuid7()

    assert publish(_event(), action_execution_id=action).action_execution_id == action


@pytest.mark.ac("AUD-01.1")
def test_event_id__is_the_same_for_the_same_event_and_differs_by_key_and_tenant() -> None:
    assert _event().event_id == _event().event_id
    assert _event().event_id != _event(idempotency_key="other").event_id
    assert _event().event_id != _event(organization_id=uuid.uuid7()).event_id


@pytest.mark.ac("AUD-01.1")
def test_publish__a_long_correlation__is_cut_to_the_outbox_column() -> None:
    assert publish(_event(correlation_id="c" * 300)).correlation_id == "c" * 200


@pytest.mark.parametrize(
    "changes",
    [
        {"event_type": "OrdersIngested"},
        {"event_type": "orders.ingested"},
        {"payload_version": 0},
        {"occurred_at": datetime(2026, 10, 26, 18, 0)},  # noqa: DTZ001 - naive on purpose
        {"payload": {"event_type": "orders.ingested.v1"}},
    ],
)
def test_domain_event__invalid__is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="event"):
        _event(**changes)


@pytest.mark.ac("AUD-01.4")
def test_read_event__an_envelope__gives_its_type_version_and_payload() -> None:
    event = read_event(publish(_event()).payload)

    assert event.event_type == "orders.ingested.v1"
    assert event.payload_version == 1
    assert event.payload == {"receipt_id": 1}
    assert event.event_id == str(_event().event_id)


@pytest.mark.ac("AUD-01.5")
def test_read_event__a_row_written_before_the_envelope__is_version_1() -> None:
    legacy: dict[str, JsonValue] = {
        "event_type": "orders.ingested.v1",
        "receipt_id": 1,
        "source": "webhook",
    }

    event = read_event(legacy)

    assert event.event_type == "orders.ingested.v1"
    assert event.payload_version == 1
    assert event.payload == {"receipt_id": 1, "source": "webhook"}
    assert event.event_id is None


@pytest.mark.ac("AUD-01.4")
def test_read_event__an_unknown_version__is_refused_with_its_type() -> None:
    envelope: dict[str, JsonValue] = {**publish(_event()).payload, "payload_version": 2}

    with pytest.raises(UnknownEventVersionError, match=r"orders\.ingested\.v1 version 2"):
        read_event(envelope)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_type": 7},
        {"event_type": "orders.ingested.v1", "payload_version": 1, "payload": "x"},
        {"event_type": "orders.ingested.v1", "payload_version": "1", "payload": {}},
    ],
)
def test_read_event__malformed__is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(MalformedEventError):
        read_event(payload)  # type: ignore[arg-type]
