"""Inbox and outbox (slice 5.2, SOFTWARE_DESIGN §8.3): events in once, effects out once.

**Inbox.** Every external event (a webhook, later an email) is recorded once by `(source,
event_id)`, together with the job that processes it, in one transaction: a duplicate delivery
finds the first row and the first job (AC-5.2.1).

**Outbox.** Every external side effect is an `OutboxMessage` written in the same transaction as
the business change (the persistence adapter's `add_outbox_in`), with a deterministic idempotency
key and its delivery job. The worker's `outbox.deliver` job calls the destination's `Deliverer`
with that key, so a crash after the business commit still delivers after restart, and a crash
after the delivery but before it was recorded repeats the call with the same key, which the
destination deduplicates (ER-REL-02). The pilot has no destination yet: HTCO writes nothing to
Etsy, and email arrives in Phase 6.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from htco_application.jobs import ClaimedJob, JobError, JobSpec, JsonValue, describe_error
from htco_domain.jobs import check_job_kind

OUTBOX_DELIVERY_JOB = "outbox.deliver"


def payload_hash(payload: Mapping[str, JsonValue]) -> bytes:
    """SHA-256 of the payload's canonical JSON."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


@dataclass(frozen=True)
class InboxEvent:
    source: str  # "etsy"
    event_id: str  # the source's own ID (for webhooks the `webhook-id`)
    event_type: str
    payload: Mapping[str, JsonValue]
    received_at: datetime
    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip() or len(self.event_id) > 200:  # noqa: PLR2004 - column size
            raise ValueError("an inbox event needs a source event ID of 1 to 200 characters")
        if self.shop_id is not None and self.organization_id is None:
            raise ValueError("a shop's event also names the shop's organization")


@dataclass(frozen=True)
class RecordedEvent:
    inbox_id: uuid.UUID
    job_id: uuid.UUID
    duplicate: bool


class Inbox(Protocol):
    def record(self, event: InboxEvent, job: JobSpec, *, now: datetime) -> RecordedEvent:
        """In one transaction: keep the event unless `(source, event_id)` exists, and enqueue
        `job` for it. A duplicate returns the first row and its job."""
        ...


@dataclass(frozen=True)
class OutboxMessage:
    organization_id: uuid.UUID
    destination: str  # `<system>.<effect>`, the deliverer that sends it
    idempotency_key: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    shop_id: uuid.UUID | None = None
    # The gateway's action this effect belongs to (ADR-0019): when the delivery succeeds or dies,
    # the action moves to VERIFYING or FAILED (slice 5.12). By convention an Etsy write's
    # `destination` is its action name, for example `etsy.inventory.update`.
    action_execution_id: uuid.UUID | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        check_job_kind(self.destination)
        if self.correlation_id is not None and not 0 < len(self.correlation_id) <= 200:  # noqa: PLR2004
            raise ValueError("a correlation ID is 1 to 200 characters")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:  # noqa: PLR2004
            raise ValueError("an outbox message needs an idempotency key of 1 to 200 characters")


@dataclass(frozen=True)
class PendingDelivery:
    id: uuid.UUID
    message: OutboxMessage
    attempts: int  # deliveries tried before this one
    delivered: bool


class Deliverer(Protocol):
    async def deliver(self, delivery: PendingDelivery) -> None:
        """Send the effect, passing `delivery.message.idempotency_key` to the destination.
        Raise to retry."""
        ...


class OutboxStore(Protocol):
    async def load(self, message_id: uuid.UUID) -> PendingDelivery | None: ...

    async def mark_delivered(self, message_id: uuid.UUID, *, now: datetime) -> None: ...

    async def record_failure(
        self, message_id: uuid.UUID, *, error: str, dead: bool, now: datetime
    ) -> None: ...


def _deliverer_for(deliverers: Mapping[str, Deliverer], pending: PendingDelivery) -> Deliverer:
    deliverer = deliverers.get(pending.message.destination)
    if deliverer is None:
        raise JobError(f"no deliverer for {pending.message.destination}")
    return deliverer


def delivery_job(message_id: uuid.UUID, message: OutboxMessage) -> JobSpec:
    return JobSpec(
        kind=OUTBOX_DELIVERY_JOB,
        idempotency_key=f"outbox:{message_id}",
        payload={"outbox_id": str(message_id)},
        organization_id=message.organization_id,
        shop_id=message.shop_id,
        correlation_id=message.correlation_id,
        causation_id=None
        if message.action_execution_id is None
        else f"action:{message.action_execution_id}",
    )


async def deliver_outbox_message(
    store: OutboxStore,
    deliverers: Mapping[str, Deliverer],
    job: ClaimedJob,
    *,
    now: datetime,
) -> bool:
    """The `outbox.deliver` job. True when this run delivered; False when it was delivered
    before (a replayed job). A failure is recorded on the message and re-raised, so the job
    retries; on the job's last attempt the message is marked dead as well."""
    raw = job.payload.get("outbox_id")
    if not isinstance(raw, str):
        raise JobError("an outbox job names its message")
    message_id = uuid.UUID(raw)
    pending = await store.load(message_id)
    if pending is None:
        raise JobError(f"no outbox message {message_id}")
    if pending.delivered:
        return False
    try:
        await _deliverer_for(deliverers, pending).deliver(pending)
    except Exception as error:
        await store.record_failure(
            message_id,
            error=describe_error(error),
            dead=job.attempts >= job.retry.max_attempts,
            now=now,
        )
        raise
    await store.mark_delivered(message_id, now=now)
    return True
