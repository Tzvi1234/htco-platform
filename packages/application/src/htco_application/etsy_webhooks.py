"""Etsy webhooks (slice 5.3): signed events in once, then a re-read of what they name.

`POST /webhooks/etsy` hands the raw request to `EtsyWebhookIngress.receive`:

1. **Verify** the Standard Webhooks signature before anything else is read: `webhook-id`,
   `webhook-timestamp` and `webhook-signature`, HMAC-SHA256 over `id.timestamp.body` with the
   endpoint's signing secret (`whsec_` and base64), a timestamp within 5 minutes of our clock, and
   a body of at most 64 KiB. Every refusal names its reason and never the body.
2. **Record** the event once in the inbox (`source = "etsy"`, `event_id = webhook-id`, the same ID
   across Etsy's retries), with the job that processes it, in one transaction. The job is keyed by
   the resource: an order event by `etsy:{shop_id}:{receipt_id}:{event_type}`, so the same order
   change announced twice (two webhook IDs) runs once. An unknown event type, or a shop that is
   not connected to HTCO, is stored with no job and counted.
3. **Answer** at once; the worker's `etsy.webhook_event` job does the rest.

The payload is never trusted: it only says *what* changed. The job re-reads the receipt (through
Etsy Order Intake, slice 5.4), the listing or its inventory through the guarded, GET-only adapter
and publishes what the re-read shows. Reconciliation (slice 5.5) stays on whatever webhooks do.

No personal data travels in an Etsy webhook (event type, shop, resource URL and IDs only), and
none is logged: log lines carry the event type, the reason and IDs.
"""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any, Protocol, override

from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.jobs import ClaimedJob, JobSpec
from htco_application.kernel import (
    DomainEvent,
    JobError,
    JsonValue,
    Offload,
    OutboxMessage,
    TenantShop,
    publish,
    run_inline,
)
from htco_application.marketplace import (
    InventorySnapshot,
    ListingSnapshot,
    MarketplaceNotFoundError,
)
from htco_application.messaging import InboxEvent, RecordedEvent
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, TriggerKind

WEBHOOK_SOURCE = "etsy"
WEBHOOK_JOB = "etsy.webhook_event"
ORDER_INTAKE = "etsy-order-intake"
ORDER_SHIPPED = "order.shipped"
# Standard Webhooks' replay window.
TOLERANCE = timedelta(minutes=5)
# Etsy's payloads are a few hundred bytes; the inbox column holds 64 KiB.
MAX_BODY_BYTES = 64 * 1024
WEBHOOK_ACTOR = ActorRef(ActorKind.SYSTEM, "etsy-webhook")

_log = logging.getLogger(__name__)


# --- Signature ---------------------------------------------------------------------------------


class Rejection(StrEnum):
    MISSING_HEADERS = "missing_headers"
    BAD_TIMESTAMP = "bad_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"
    BAD_SIGNATURE = "bad_signature"
    TOO_LARGE = "too_large"
    BAD_PAYLOAD = "bad_payload"


class WebhookRejectedError(Exception):
    """A refused webhook. `reason` is safe to log; nothing of the body is in it."""

    def __init__(self, reason: Rejection) -> None:
        super().__init__(f"webhook rejected: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, repr=False)
class WebhookSecret:
    """The endpoint's signing secret. Never in a repr, a log line or an error."""

    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) < 16:  # noqa: PLR2004 - Standard Webhooks secrets are 24 to 64 bytes
            raise ValueError("a webhook signing secret has at least 16 bytes")

    @classmethod
    def parse(cls, text: str) -> WebhookSecret:
        """`whsec_` followed by base64, as the Etsy developer portal shows it."""
        raw = text.strip()
        raw = raw.removeprefix("whsec_")
        try:
            key = base64.b64decode(raw, validate=True)
        except binascii.Error, ValueError:
            raise ValueError("the webhook signing secret is not whsec_ and base64") from None
        return cls(key)

    def sign(self, webhook_id: str, timestamp: str, body: bytes) -> str:
        content = f"{webhook_id}.{timestamp}.".encode() + body
        digest = hmac.new(self.key, content, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    @override
    def __repr__(self) -> str:
        return "WebhookSecret(<hidden>)"


@dataclass(frozen=True)
class SignedRequest:
    webhook_id: str
    timestamp: datetime
    raw_timestamp: str


def has_signature_headers(headers: Mapping[str, str]) -> bool:
    """`webhook-id` (at most 200 characters), `webhook-timestamp` and `webhook-signature` are
    all present and non-empty. Checked before the body is read: without them, refused at once."""
    lower = {name.lower(): value for name, value in headers.items()}
    webhook_id = lower.get("webhook-id", "").strip()
    return (
        0 < len(webhook_id) <= 200  # noqa: PLR2004
        and bool(lower.get("webhook-timestamp", "").strip())
        and bool(lower.get("webhook-signature", "").strip())
    )


def verify_signature(
    secret: WebhookSecret,
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: datetime,
    tolerance: timedelta = TOLERANCE,
) -> SignedRequest:
    """Standard Webhooks verification on the raw body. Raises `WebhookRejectedError`."""
    if len(body) > MAX_BODY_BYTES:
        raise WebhookRejectedError(Rejection.TOO_LARGE)
    if not has_signature_headers(headers):
        raise WebhookRejectedError(Rejection.MISSING_HEADERS)
    lower = {name.lower(): value for name, value in headers.items()}
    webhook_id = lower["webhook-id"].strip()
    raw_timestamp = lower["webhook-timestamp"].strip()
    signatures = lower["webhook-signature"].strip()
    if not raw_timestamp.isdigit() or len(raw_timestamp) > 12:  # noqa: PLR2004
        raise WebhookRejectedError(Rejection.BAD_TIMESTAMP)
    sent = datetime.fromtimestamp(int(raw_timestamp), UTC)
    if abs(now - sent) > tolerance:
        raise WebhookRejectedError(Rejection.STALE_TIMESTAMP)
    expected = secret.sign(webhook_id, raw_timestamp, body)
    # "v1,<base64> v1,<base64>": any v1 entry may match (secret rotation sends two).
    for entry in signatures.split():
        version, _, value = entry.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected.encode()):
            return SignedRequest(webhook_id, sent, raw_timestamp)
    raise WebhookRejectedError(Rejection.BAD_SIGNATURE)


# --- Events and routes ------------------------------------------------------------------------


class ResourceKind(StrEnum):
    ORDER = "order"
    LISTING = "listing"
    INVENTORY = "inventory"


@dataclass(frozen=True)
class Route:
    """Where an event type goes in Phase 5 (phase-05 §5.3)."""

    resource: ResourceKind
    # Events published once the re-read confirms the change (EVENT_CATALOG).
    publishes: tuple[str, ...] = ()


LISTING_CHANGED = "catalog.listing_changed.v1"
INVENTORY_CHANGED = "catalog.inventory_changed.v1"
OUT_OF_STOCK = "catalog.out_of_stock.v1"

ROUTES: Mapping[str, Route] = {
    # Etsy Order Intake re-reads the receipt and emits the orders.* events (slice 5.4).
    "order.paid": Route(ResourceKind.ORDER),
    "order.canceled": Route(ResourceKind.ORDER),
    "order.shipped": Route(ResourceKind.ORDER),
    "order.delivered": Route(ResourceKind.ORDER),
    "listing.created": Route(ResourceKind.LISTING, (LISTING_CHANGED,)),
    "listing.updated": Route(ResourceKind.LISTING, (LISTING_CHANGED,)),
    "listing.deleted": Route(ResourceKind.LISTING, (LISTING_CHANGED,)),
    "inventory.created": Route(ResourceKind.INVENTORY, (INVENTORY_CHANGED,)),
    "inventory.updated": Route(ResourceKind.INVENTORY, (INVENTORY_CHANGED,)),
    "inventory.deleted": Route(ResourceKind.INVENTORY, (INVENTORY_CHANGED,)),
    "inventory.out_of_stock": Route(ResourceKind.INVENTORY, (INVENTORY_CHANGED, OUT_OF_STOCK)),
}

_EVENT_TYPE = re.compile(r"^[a-z][a-z_]{0,40}\.[a-z][a-z_]{0,40}$")
_RECEIPT_IN_URL = re.compile(r"/receipts/(\d{1,19})(?:[/?#]|$)")
_LISTING_IN_URL = re.compile(r"/listings/(\d{1,19})(?:[/?#]|$)")
_PRODUCT_IN_URL = re.compile(r"/products/(\d{1,19})(?:[/?#]|$)")


@dataclass(frozen=True)
class EtsyWebhookEvent:
    """What an Etsy webhook says changed: the type, the Etsy shop and the resource's IDs."""

    event_type: str
    etsy_shop_id: int
    resource_url: str | None
    receipt_id: int | None = None
    listing_id: int | None = None
    product_id: int | None = None

    @property
    def route(self) -> Route | None:
        return ROUTES.get(self.event_type)

    @property
    def resource_id(self) -> int | None:
        route = self.route
        if route is None:
            return None
        if route.resource is ResourceKind.ORDER:
            return self.receipt_id
        return self.listing_id

    def stored_payload(self) -> dict[str, JsonValue]:
        """What the inbox keeps: the event's IDs (Etsy sends nothing else)."""
        payload: dict[str, JsonValue] = {
            "event_type": self.event_type,
            "shop_id": self.etsy_shop_id,
            "resource_url": self.resource_url,
        }
        for name in ("receipt_id", "listing_id", "product_id"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


def _positive(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and 0 < len(value) <= 19:  # noqa: PLR2004
        return int(value)
    return None


def _from_url(pattern: re.Pattern[str], url: str | None) -> int | None:
    if url is None:
        return None
    found = pattern.search(url)
    return None if found is None else int(found.group(1))


def parse_event(body: bytes) -> EtsyWebhookEvent:
    """The event from a verified body. Raises `WebhookRejectedError(BAD_PAYLOAD)`."""
    try:
        data: Any = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD) from None
    if not isinstance(data, dict):
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    event_type = data.get("event_type")
    shop_id = _positive(data.get("shop_id"))
    url = data.get("resource_url")
    if not isinstance(event_type, str) or not _EVENT_TYPE.match(event_type) or shop_id is None:
        raise WebhookRejectedError(Rejection.BAD_PAYLOAD)
    resource_url = url[:500] if isinstance(url, str) else None
    return EtsyWebhookEvent(
        event_type=event_type,
        etsy_shop_id=shop_id,
        resource_url=resource_url,
        receipt_id=_positive(data.get("receipt_id")) or _from_url(_RECEIPT_IN_URL, resource_url),
        listing_id=_positive(data.get("listing_id")) or _from_url(_LISTING_IN_URL, resource_url),
        product_id=_positive(data.get("product_id")) or _from_url(_PRODUCT_IN_URL, resource_url),
    )


# --- Ingress (the API process) ----------------------------------------------------------------


class WebhookShops(Protocol):
    async def connected_shop(self, etsy_shop_id: int) -> TenantShop | None:
        """The HTCO shop connected to this Etsy shop, or None."""
        ...


class WebhookInbox(Protocol):
    def record(self, event: InboxEvent, job: JobSpec, *, now: datetime) -> RecordedEvent: ...

    def keep(self, event: InboxEvent, *, now: datetime) -> tuple[uuid.UUID, bool]:
        """Store an event that no job processes (unknown type or shop): its ID and whether it
        was a duplicate."""
        ...


@dataclass(frozen=True)
class Accepted:
    inbox_id: uuid.UUID
    duplicate: bool
    job_id: uuid.UUID | None  # None: stored, not processed (unknown type or shop)


def webhook_job(event: EtsyWebhookEvent, shop: TenantShop, signed: SignedRequest) -> JobSpec:
    """The processing job, keyed by the resource so one change announced twice runs once."""
    route = event.route
    if route is None or event.resource_id is None:  # pragma: no cover - checked by the caller
        raise ValueError("only routed events with a resource have a job")
    if route.resource is ResourceKind.ORDER:
        key = f"etsy:{shop.shop_id}:{event.receipt_id}:{event.event_type}"
    else:
        # Listings and stock change many times: the same change (same second) once.
        product = "" if event.product_id is None else f":{event.product_id}"
        key = (
            f"etsy:{shop.shop_id}:{event.listing_id}{product}:{event.event_type}"
            f"@{signed.raw_timestamp}"
        )
    payload: dict[str, JsonValue] = {
        k: v for k, v in event.stored_payload().items() if k != "resource_url"
    }
    payload["webhook_id"] = signed.webhook_id
    return JobSpec(
        kind=WEBHOOK_JOB,
        idempotency_key=key[:200],
        payload=payload,
        organization_id=shop.organization_id,
        shop_id=shop.shop_id,
        correlation_id=f"etsy-webhook:{signed.webhook_id}"[:200],
    )


@dataclass
class EtsyWebhookIngress:
    secret: WebhookSecret
    inbox: WebhookInbox
    shops: WebhookShops
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    # Events stored without a job, by reason (the 5.9 metrics read these).
    unrouted: Counter[str] = field(default_factory=Counter)

    def verify(self, headers: Mapping[str, str], body: bytes) -> SignedRequest:
        """Step 1 alone: size, headers, timestamp and signature. Raises `WebhookRejectedError`."""
        return verify_signature(self.secret, headers, body, now=self.clock())

    async def receive(self, headers: Mapping[str, str], body: bytes) -> Accepted:
        return await self.accept(self.verify(headers, body), body)

    async def accept(self, signed: SignedRequest, body: bytes) -> Accepted:
        """Steps 2 and 3, for a body `verify` has just accepted."""
        now = self.clock()
        event = parse_event(body)
        shop = await self.shops.connected_shop(event.etsy_shop_id)
        inbox_event = InboxEvent(
            source=WEBHOOK_SOURCE,
            event_id=signed.webhook_id,
            event_type=event.event_type[:100],
            payload=event.stored_payload(),
            received_at=now,
            organization_id=None if shop is None else shop.organization_id,
            shop_id=None if shop is None else shop.shop_id,
        )
        reason = _unrouted_reason(event, shop)
        if reason is not None or shop is None:
            inbox_id, duplicate = await self.offload(lambda: self.inbox.keep(inbox_event, now=now))
            if not duplicate:
                self.unrouted[reason or "unknown_shop"] += 1
            _log.info(
                "etsy.webhook_stored_unrouted: %s %s duplicate=%s",
                event.event_type,
                reason or "unknown_shop",
                duplicate,
            )
            return Accepted(inbox_id=inbox_id, duplicate=duplicate, job_id=None)
        job = webhook_job(event, shop, signed)
        recorded: RecordedEvent = await self.offload(
            lambda: self.inbox.record(inbox_event, job, now=now)
        )
        _log.info(
            "etsy.webhook_received: %s shop=%s duplicate=%s",
            event.event_type,
            shop.shop_id,
            recorded.duplicate,
        )
        return Accepted(recorded.inbox_id, recorded.duplicate, recorded.job_id)


def _unrouted_reason(event: EtsyWebhookEvent, shop: TenantShop | None) -> str | None:
    if event.route is None:
        return "unknown_event_type"
    if shop is None:
        return "unknown_shop"
    if event.resource_id is None:
        return "no_resource_id"
    return None


# --- Per-source rate limit (the API process) --------------------------------------------------


def ip_source(ip: str | None) -> str:
    """The per-IP limiter's key: an IPv4 address as is, an IPv6 one by its /64 (one host or
    customer usually holds a whole /64, so per address it could rotate freely), and a missing or
    unparsable value as one shared `ip:unknown` bucket."""
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return "ip:unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return f"ip:{address.ipv4_mapped}"
        return f"ip:{ipaddress.IPv6Network((address, 64), strict=False)}"
    return f"ip:{address}"


@dataclass
class WindowLimiter:
    """At most `limit` requests per `window` for each source; the rest get 429 with the seconds
    until the window ends. In memory per API instance: a guard against floods, not a budget.

    `POST /webhooks/etsy` uses two: one per client IP (`ip_source`) that counts only requests
    failing before or at the signature check, and the Etsy-wide one (source `"etsy"`) charged
    only once the signature verifies. A forged IP header can therefore never block a signed
    delivery, and unsigned traffic cannot spend Etsy's allowance.

    The table holds at most `max_sources` windows and keeps them oldest-start first. Ended windows
    are pruned once per `window`, from the front. A new source is never refused because the table
    is full: ended windows are evicted first, and if every window is still live the oldest one is
    forgotten (that source starts a fresh window). Memory stays bounded; a flood wider than
    `max_sources` can only make the limiter forget, never block a newcomer."""

    limit: int
    window: timedelta = timedelta(seconds=1)
    max_sources: int = 10_000
    _windows: dict[str, tuple[datetime, int]] = field(default_factory=dict)
    _next_prune: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, source: str, *, now: datetime) -> float | None:
        """None when admitted; otherwise the seconds to wait."""
        with self._lock:
            if self._next_prune is None or now >= self._next_prune:
                self._drop_ended(now)
                self._next_prune = now + self.window
            start, used = self._windows.get(source, (now, 0))
            if now - start >= self.window:
                start, used = now, 0
            if used >= self.limit:
                return max(0.0, (start + self.window - now).total_seconds())
            if used == 0:
                # A new window goes to the end, so the table stays ordered by start.
                self._windows.pop(source, None)
                if len(self._windows) >= self.max_sources:
                    self._drop_ended(now)
                if len(self._windows) >= self.max_sources:
                    del self._windows[next(iter(self._windows))]
            self._windows[source] = (start, used + 1)
            return None

    def _drop_ended(self, now: datetime) -> None:
        """Ended windows sit at the front: drop them until the first live one."""
        while self._windows:
            oldest = next(iter(self._windows))
            if now - self._windows[oldest][0] < self.window:
                return
            del self._windows[oldest]


# --- Processing (the worker) ------------------------------------------------------------------


class CatalogReads(Protocol):
    """The guarded adapter's GETs the re-read uses."""

    async def get_listing(self, shop_id: int, listing_id: int) -> ListingSnapshot: ...

    async def get_listing_inventory(self, shop_id: int, listing_id: int) -> InventorySnapshot: ...


class TenantEtsyShops(Protocol):
    async def etsy_shop_of(self, shop: TenantShop) -> int | None:
        """The Etsy shop connected to the HTCO shop (inside its organization), or None."""
        ...


class EventOutbox(Protocol):
    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID: ...


class ProcessedMarks(Protocol):
    def mark_processed(self, job_id: uuid.UUID, *, now: datetime) -> int:
        """Every inbox event handled by this job is processed now; returns how many."""
        ...


class OrderIntakeTrigger(Protocol):
    async def receipt_event(
        self,
        shop: TenantShop,
        *,
        receipt_id: int,
        event_type: str,
        correlation_id: str,
        causation_id: str | None,
    ) -> bool:
        """Start Etsy Order Intake for the receipt; False when the automation refused it."""
        ...


class TriggersAutomations(Protocol):
    def trigger(self, request: TriggerRequest) -> AutomationExecution: ...


@dataclass
class EngineOrderIntake:
    """`OrderIntakeTrigger` on the durable engine. Until the owner activates Etsy Order Intake
    (slice 5.11) the engine refuses it; the refusal is logged and reconciliation covers the
    order, so the webhook job never retries in a loop."""

    engine: TriggersAutomations
    offload: Offload = run_inline

    async def receipt_event(
        self,
        shop: TenantShop,
        *,
        receipt_id: int,
        event_type: str,
        correlation_id: str,
        causation_id: str | None,
    ) -> bool:
        request = TriggerRequest(
            automation_id=ORDER_INTAKE,
            trigger=TriggerKind.EVENT,
            actor=WEBHOOK_ACTOR,
            idempotency_key=f"etsy:{shop.shop_id}:{receipt_id}:{event_type}",
            payload={"receipt_id": receipt_id, "event_type": event_type},
            correlation_id=correlation_id,
            organization_id=shop.organization_id,
            shop_id=shop.shop_id,
            causation_id=causation_id,
        )
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("etsy.webhook_order_not_ingested: %s shop=%s", event_type, shop.shop_id)
            return False
        return True


@dataclass
class WebhookProcessor:
    """The `etsy.webhook_event` job: never trusts the payload, always re-reads."""

    orders: OrderIntakeTrigger
    catalog: CatalogReads
    shops: TenantEtsyShops
    outbox: EventOutbox
    marks: ProcessedMarks
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    # Tracking Synchronization (slice 6.1): also started by `order.shipped`, beside the intake.
    tracking: OrderIntakeTrigger | None = None

    async def process(self, job: ClaimedJob) -> str:
        """The outcome: "ingest_triggered", "ingest_refused", "published", "skipped"."""
        if job.organization_id is None or job.shop_id is None:
            raise JobError("a webhook job names its shop and organization")
        shop = TenantShop(job.organization_id, job.shop_id)
        event_type = job.payload.get("event_type")
        route = ROUTES.get(event_type) if isinstance(event_type, str) else None
        if route is None or not isinstance(event_type, str):
            raise JobError("a webhook job names a known event type")
        correlation = job.correlation_id or f"job:{job.id}"
        if route.resource is ResourceKind.ORDER:
            receipt_id = _positive(job.payload.get("receipt_id"))
            if receipt_id is None:
                raise JobError("an order webhook job names its receipt")
            started = await self.orders.receipt_event(
                shop,
                receipt_id=receipt_id,
                event_type=event_type,
                correlation_id=correlation,
                causation_id=f"job:{job.id}",
            )
            outcome = "ingest_triggered" if started else "ingest_refused"
            if event_type == ORDER_SHIPPED and self.tracking is not None:
                await self.tracking.receipt_event(
                    shop,
                    receipt_id=receipt_id,
                    event_type=event_type,
                    correlation_id=correlation,
                    causation_id=f"job:{job.id}",
                )
        else:
            outcome = await self._catalog(job, shop, event_type, route, correlation)
        now = self.clock()
        await self.offload(lambda: self.marks.mark_processed(job.id, now=now))
        return outcome

    async def _catalog(
        self, job: ClaimedJob, shop: TenantShop, event_type: str, route: Route, correlation: str
    ) -> str:
        listing_id = _positive(job.payload.get("listing_id"))
        if listing_id is None:
            raise JobError("a catalog webhook job names its listing")
        etsy_shop = await self.shops.etsy_shop_of(shop)
        if etsy_shop is None:
            return "skipped"  # disconnected meanwhile: nothing to read
        facts = await self._reread(route, etsy_shop, listing_id, job.payload.get("product_id"))
        if facts is None:
            _log.warning(
                "etsy.webhook_listing_of_another_shop: %s shop=%s", event_type, shop.shop_id
            )
            return "skipped"
        now = self.clock()
        for name in route.publishes:
            if name == OUT_OF_STOCK and facts.get("in_stock") is not False:
                continue  # the re-read does not confirm it: never trust the payload
            message = publish(
                DomainEvent(
                    event_type=name,
                    organization_id=shop.organization_id,
                    shop_id=shop.shop_id,
                    idempotency_key=f"{name}:{job.id}",
                    occurred_at=now,
                    actor=WEBHOOK_ACTOR,
                    payload={"webhook_event_type": event_type, **facts},
                    correlation_id=correlation,
                    causation_id=f"job:{job.id}",
                )
            )
            await self.offload(partial(self.outbox.add, message, now=now))
        return "published"

    async def _reread(
        self, route: Route, etsy_shop: int, listing_id: int, raw_product: JsonValue
    ) -> dict[str, JsonValue] | None:
        """What Etsy says now; None when the listing belongs to another shop."""
        try:
            listing = await self.catalog.get_listing(etsy_shop, listing_id)
        except MarketplaceNotFoundError:
            listing = None
        if listing is not None and listing.etsy_shop_id != etsy_shop:
            return None
        facts: dict[str, JsonValue] = {
            "listing_id": listing_id,
            "state": "deleted" if listing is None else listing.state,
            "listing_quantity": None if listing is None else listing.quantity,
            "etsy_updated_at": None if listing is None else listing.updated_at.isoformat(),
        }
        if route.resource is not ResourceKind.INVENTORY:
            return facts
        product_id = _positive(raw_product)
        inventory = None
        if listing is not None:
            try:
                inventory = await self.catalog.get_listing_inventory(etsy_shop, listing_id)
            except MarketplaceNotFoundError:
                inventory = None
        products = () if inventory is None else inventory.products
        product = next((p for p in products if p.product_id == product_id), None)
        quantity = (
            product.quantity
            if product is not None
            else (None if inventory is None else inventory.total_quantity)
        )
        facts |= {
            "product_id": product_id,
            "product_found": product is not None,
            "products": len(products),
            "total_quantity": None if inventory is None else inventory.total_quantity,
            "in_stock": None if quantity is None else quantity > 0,
        }
        return facts
