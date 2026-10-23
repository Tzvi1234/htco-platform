"""Inbox and outbox values and the outbox delivery job (slice 5.2), with in-memory fakes."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.jobs import DEFAULT_RETRY, ClaimedJob, JobError
from htco_application.messaging import (
    OUTBOX_DELIVERY_JOB,
    InboxEvent,
    OutboxMessage,
    PendingDelivery,
    deliver_outbox_message,
    delivery_job,
    payload_hash,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("1a000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("1b000000-0000-0000-0000-000000000001")


@dataclass
class Store:
    rows: dict[uuid.UUID, PendingDelivery] = field(default_factory=dict)
    failures: list[tuple[uuid.UUID, str, bool]] = field(default_factory=list)

    def add(self, message: OutboxMessage) -> uuid.UUID:
        message_id = uuid.uuid7()
        self.rows[message_id] = PendingDelivery(message_id, message, attempts=0, delivered=False)
        return message_id

    async def load(self, message_id: uuid.UUID) -> PendingDelivery | None:
        return self.rows.get(message_id)

    async def mark_delivered(self, message_id: uuid.UUID, *, now: datetime) -> None:  # noqa: ARG002 - the port
        row = self.rows[message_id]
        self.rows[message_id] = replace(row, delivered=True, attempts=row.attempts + 1)

    async def record_failure(
        self,
        message_id: uuid.UUID,
        *,
        error: str,
        dead: bool,
        now: datetime,  # noqa: ARG002 - the port
    ) -> None:
        self.failures.append((message_id, error, dead))


@dataclass
class Sink:
    """A destination that deduplicates by idempotency key, like a well-behaved external API."""

    received: dict[str, int] = field(default_factory=dict)
    fail: int = 0

    async def deliver(self, delivery: PendingDelivery) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("destination down")
        key = delivery.message.idempotency_key
        self.received[key] = self.received.get(key, 0) + 1


def message(**changes: object) -> OutboxMessage:
    base = OutboxMessage(
        organization_id=ORG,
        shop_id=SHOP,
        destination="test.sink",
        idempotency_key="sink:receipt-1:paid",
        payload={"receipt_id": "1"},
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def job_for(message_id: uuid.UUID, *, attempts: int = 1) -> ClaimedJob:
    spec = delivery_job(message_id, message())
    return ClaimedJob(
        id=uuid.uuid7(),
        kind=spec.kind,
        idempotency_key=spec.idempotency_key,
        payload=dict(spec.payload),
        organization_id=spec.organization_id,
        shop_id=spec.shop_id,
        lock_key=None,
        attempts=attempts,
        retry=DEFAULT_RETRY,
        lease_expires_at=NOW + timedelta(minutes=1),
    )


def test_payload_hash__ignores_key_order_and_changes_with_content() -> None:
    assert payload_hash({"a": 1, "b": [1, 2]}) == payload_hash({"b": [1, 2], "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})
    assert len(payload_hash({})) == 32


@pytest.mark.parametrize("event_id", ["", "  ", "e" * 201])
def test_inbox_event__blank_or_long_event_id__is_rejected(event_id: str) -> None:
    with pytest.raises(ValueError, match="event ID"):
        InboxEvent("etsy", event_id, "receipt.updated", {}, NOW)


def test_inbox_event__shop_without_organization__is_rejected() -> None:
    with pytest.raises(ValueError, match="organization"):
        InboxEvent("etsy", "evt-1", "receipt.updated", {}, NOW, shop_id=SHOP)


def test_outbox_message__invalid_destination_or_key__is_rejected() -> None:
    with pytest.raises(ValueError, match="job kind"):
        message(destination="Sink")
    with pytest.raises(ValueError, match="idempotency key"):
        message(idempotency_key=" ")


def test_delivery_job__keyed_by_the_message_in_its_organization() -> None:
    message_id = uuid.uuid7()

    spec = delivery_job(message_id, message())

    assert (spec.kind, spec.idempotency_key) == (OUTBOX_DELIVERY_JOB, f"outbox:{message_id}")
    assert (spec.organization_id, spec.shop_id) == (ORG, SHOP)
    assert spec.payload == {"outbox_id": str(message_id)}


@pytest.mark.ac("AC-5.2.2")
async def test_deliver__replayed_job__delivers_once() -> None:
    store, sink = Store(), Sink()
    message_id = store.add(message())

    first = await deliver_outbox_message(store, {"test.sink": sink}, job_for(message_id), now=NOW)
    again = await deliver_outbox_message(store, {"test.sink": sink}, job_for(message_id), now=NOW)

    assert (first, again) == (True, False)
    assert sink.received == {"sink:receipt-1:paid": 1}
    assert store.rows[message_id].delivered


async def test_deliver__destination_down__records_the_failure_and_raises_to_retry() -> None:
    store, sink = Store(), Sink(fail=1)
    message_id = store.add(message())

    with pytest.raises(ConnectionError):
        await deliver_outbox_message(store, {"test.sink": sink}, job_for(message_id), now=NOW)

    assert store.failures == [(message_id, "ConnectionError", False)]
    assert not store.rows[message_id].delivered


async def test_deliver__failing_on_the_jobs_last_attempt__marks_the_message_dead() -> None:
    store, sink = Store(), Sink(fail=1)
    message_id = store.add(message())

    with pytest.raises(ConnectionError):
        await deliver_outbox_message(
            store, {"test.sink": sink}, job_for(message_id, attempts=3), now=NOW
        )

    assert store.failures == [(message_id, "ConnectionError", True)]


async def test_deliver__no_deliverer_for_the_destination__fails_with_a_safe_error() -> None:
    store = Store()
    message_id = store.add(message())

    with pytest.raises(JobError, match="no deliverer"):
        await deliver_outbox_message(store, {}, job_for(message_id), now=NOW)

    assert store.failures == [(message_id, "JobError: no deliverer for test.sink", False)]


async def test_deliver__unknown_or_unnamed_message__fails() -> None:
    with pytest.raises(JobError, match="no outbox message"):
        await deliver_outbox_message(Store(), {}, job_for(uuid.uuid7()), now=NOW)
    with pytest.raises(JobError, match="names its message"):
        await deliver_outbox_message(
            Store(), {}, replace(job_for(uuid.uuid7()), payload={}), now=NOW
        )


def test_delivery_job__carries_the_actions_correlation_and_causation() -> None:
    """ADR-0019: trigger, job, action and outbox share the correlation ID."""
    action = uuid.uuid7()
    message_id = uuid.uuid7()

    spec = delivery_job(message_id, message(action_execution_id=action, correlation_id="req-5"))

    assert (spec.correlation_id, spec.causation_id) == ("req-5", f"action:{action}")


def test_outbox_message__long_correlation__is_rejected() -> None:
    with pytest.raises(ValueError, match="correlation"):
        message(correlation_id="c" * 201)
