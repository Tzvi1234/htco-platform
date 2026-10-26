"""Etsy webhook helpers and in-memory ports with the Postgres rules (slice 5.3), shared by the
application, API and worker tests. All values are invented."""

# ruff: noqa: ARG002 - the ports' signatures

import base64
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from htco_application.etsy_webhooks import WebhookSecret
from htco_application.jobs import JobSpec
from htco_application.kernel import TenantShop
from htco_application.messaging import InboxEvent, OutboxMessage, RecordedEvent
from tests.job_fakes import MemoryJobQueue

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "etsy" / "webhooks" / "events.json"
# A fake secret: 32 bytes of a repeated byte, never a real one.
SECRET_BYTES = bytes(range(32))
SECRET_TEXT = "whsec_" + base64.b64encode(SECRET_BYTES).decode("ascii")
SECRET = WebhookSecret.parse(SECRET_TEXT)
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
ETSY_SHOP = 10000001
ORG = uuid.UUID("5e000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("5f000000-0000-0000-0000-000000000001")
TENANT = TenantShop(ORG, SHOP)


def recorded_events() -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]
    return events


def signed(
    body: bytes,
    *,
    webhook_id: str = "msg_fake0001",
    at: datetime = NOW,
    secret: WebhookSecret = SECRET,
) -> dict[str, str]:
    timestamp = str(int(at.timestamp()))
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": "v1," + secret.sign(webhook_id, timestamp, body),
        "content-type": "application/json",
    }


def body_of(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode()


@dataclass
class MemoryWebhookInbox:
    """`jobs.inbox` with its unique `(source, event_id)` and the job in the same step."""

    queue: MemoryJobQueue = field(default_factory=MemoryJobQueue)
    rows: dict[tuple[str, str], tuple[uuid.UUID, InboxEvent, uuid.UUID | None]] = field(
        default_factory=dict
    )
    processed: dict[uuid.UUID, datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, event: InboxEvent, job: JobSpec, *, now: datetime) -> RecordedEvent:
        with self._lock:
            key = (event.source, event.event_id)
            found = self.rows.get(key)
            if found is not None and found[2] is not None:
                return RecordedEvent(inbox_id=found[0], job_id=found[2], duplicate=True)
            job_id = self.queue.enqueue(job, now=now)
            inbox_id = uuid.uuid7()
            self.rows[key] = (inbox_id, event, job_id)
            return RecordedEvent(inbox_id=inbox_id, job_id=job_id, duplicate=False)

    def keep(self, event: InboxEvent, *, now: datetime) -> tuple[uuid.UUID, bool]:
        with self._lock:
            key = (event.source, event.event_id)
            found = self.rows.get(key)
            if found is not None:
                return found[0], True
            inbox_id = uuid.uuid7()
            self.rows[key] = (inbox_id, event, None)
            return inbox_id, False

    def mark_processed(self, job_id: uuid.UUID, *, now: datetime) -> int:
        with self._lock:
            count = 0
            for inbox_id, _, job in self.rows.values():
                if job == job_id and inbox_id not in self.processed:
                    self.processed[inbox_id] = now
                    count += 1
            return count


@dataclass
class KnownShops:
    shops: dict[int, TenantShop] = field(default_factory=lambda: {ETSY_SHOP: TENANT})

    async def connected_shop(self, etsy_shop_id: int) -> TenantShop | None:
        return self.shops.get(etsy_shop_id)

    async def etsy_shop_of(self, shop: TenantShop) -> int | None:
        return next((etsy for etsy, known in self.shops.items() if known == shop), None)


@dataclass
class MemoryOutbox:
    messages: dict[str, OutboxMessage] = field(default_factory=dict)

    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID:
        self.messages.setdefault(message.idempotency_key, message)
        return uuid.uuid5(uuid.NAMESPACE_URL, message.idempotency_key)
