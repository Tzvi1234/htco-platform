"""Tracking Synchronization (slice 6.1, FR-ORD-06): the `tracking-sync` handler.

Tracking reaches Etsy from AutoVA (the owner copies it today, A-05; the relay of slice 6.10 later).
This automation only **reads**: the receipt through the Action Gateway (`etsy.receipts.read`, the
GET-only API channel, in its order history form, which carries the shipments with their codes).
Each shipment becomes one `tracking_events` row, keyed by the shop and the code's fingerprint, so
a receipt with several shipments records each once and a repeated observation adds nothing
(AC-6.1.1). The row keeps when tracking first appeared and from which source and trigger
(AC-6.1.2), the carrier and the code's last four characters; the full code stays in the buyer
table of the order history, which only buyer-data roles read.

A new tracking event writes `fulfillment.tracking_detected.v1` with it, and resolves the order's
open `tracking_missing` incident on that evidence (AC-6.3.2). The order's move to `SHIPPED` is
Etsy Order Intake's (its state machine, slice 5.4): this automation never transitions the order
and never writes to Etsy (AC-6.7.2).

Two kinds of run:
- **A receipt** (the `order.shipped` webhook, `receipt_id` in the payload);
- **The schedule** (`mode = "schedule"`, every 15 minutes per shop): the stored orders that Etsy
  marks shipped but that have no tracking event yet, a bounded batch per run.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import WEBHOOK_ACTOR, TriggersAutomations
from htco_application.fulfillment_schedule import TRACKING_SYNC
from htco_application.incidents import EVENTS, IncidentService, order_resource
from htco_application.jobs import JobError, JsonValue
from htco_application.maintenance import TenantShop
from htco_application.marketplace import OrderReceipt
from htco_application.messaging import OutboxMessage
from htco_application.order_intake import GatewayReceipts, IntakeShops
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.fulfillment import carrier_of, normalize_tracking_code, tracking_fingerprint
from htco_domain.incidents import Cause, Evidence, EvidenceKind, ResourceKind, dedup_key
from htco_domain.integrations import IntegrationStatus

TRACKING_DETECTED = "fulfillment.tracking_detected.v1"
SCHEDULE_BATCH = 25

_log = logging.getLogger(__name__)


class TrackingSource(StrEnum):
    """Where the tracking number came from."""

    ETSY = "etsy"  # Etsy's receipt (written there by AutoVA or by hand)
    AUTOVA_MANUAL = "autova_manual"  # pasted by the owner from AutoVA (slice 6.10)
    AUTOVA_BROWSER = "autova_browser"  # recipe A-01 (Phase 8A)
    AUTOVA_EMAIL = "autova_email"  # AutoVA's email (Phase 14)


class ObservedVia(StrEnum):
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    RELAY = "relay"


@dataclass(frozen=True)
class TrackingObservation:
    """One shipment as observed. No buyer data: the code only as fingerprint and last four."""

    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    receipt_id: int
    carrier: str  # ours (`Carrier`)
    carrier_name: str  # as Etsy names it
    fingerprint: bytes
    last4: str
    source: TrackingSource
    observed_via: ObservedVia
    observed_at: datetime
    notified_at: datetime | None
    execution_id: uuid.UUID | None
    correlation_id: str | None


@dataclass(frozen=True)
class RecordedTracking:
    id: uuid.UUID
    created: bool
    first_observed_at: datetime
    source: TrackingSource


class TrackingStore(Protocol):
    """Synchronous (psycopg's pool); run through `offload`."""

    def record_tracking(
        self, observation: TrackingObservation, event: OutboxMessage, *, now: datetime
    ) -> RecordedTracking:
        """Insert unless the shop already has this fingerprint for the receipt; the event is
        written only with a new row, in its transaction."""
        ...

    def untracked_shipped(self, shop_id: uuid.UUID, *, limit: int) -> tuple[int, ...]:
        """Receipts Etsy marks shipped that have no tracking event yet, oldest first."""
        ...


type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


def tracking_event(observation: TrackingObservation) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "event_type": TRACKING_DETECTED,
        "tracking_event_id": str(observation.id),
        "receipt_id": observation.receipt_id,
        "carrier": observation.carrier,
        "source": observation.source.value,
        "observed_via": observation.observed_via.value,
        "observed_at": observation.observed_at.isoformat(),
    }
    return OutboxMessage(
        organization_id=observation.organization_id,
        shop_id=observation.shop_id,
        destination=EVENTS,
        idempotency_key=(
            f"{TRACKING_DETECTED}:{observation.shop_id}:{observation.receipt_id}:"
            f"{observation.fingerprint.hex()[:32]}"
        ),
        payload=payload,
        correlation_id=None
        if observation.correlation_id is None
        else observation.correlation_id[:200],
    )


@dataclass
class TrackingRecorder:
    """Records observed shipments and resolves the order's missing-tracking incident: shared by
    Tracking Synchronization and the AutoVA relay (slice 6.10)."""

    store: TrackingStore
    incidents: IncidentService
    clock: Callable[[], datetime]
    new_id: Callable[[], uuid.UUID] = uuid.uuid7
    offload: Offload = _inline

    async def record(  # noqa: PLR0913 - one shipment's facts
        self,
        *,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        receipt_id: int,
        carrier_name: str,
        code: str,
        source: TrackingSource,
        observed_via: ObservedVia,
        notified_at: datetime | None,
        actor: ActorRef,
        execution_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> RecordedTracking:
        now = self.clock()
        normalized = normalize_tracking_code(code)
        if not normalized:
            raise JobError("a shipment without a tracking code")
        observation = TrackingObservation(
            id=self.new_id(),
            organization_id=organization_id,
            shop_id=shop_id,
            receipt_id=receipt_id,
            carrier=carrier_of(carrier_name).value,
            carrier_name=carrier_name[:64],
            fingerprint=tracking_fingerprint(shop_id, normalized),
            last4=normalized[-4:],
            source=source,
            observed_via=observed_via,
            observed_at=now,
            notified_at=notified_at,
            execution_id=execution_id,
            correlation_id=correlation_id,
        )
        event = tracking_event(observation)
        recorded: RecordedTracking = await self.offload(
            lambda: self.store.record_tracking(observation, event, now=now)
        )
        await self.incidents.resolve_on_evidence(
            organization_id,
            dedup_key(
                Cause.TRACKING_MISSING, ResourceKind.ORDER, order_resource(shop_id, receipt_id)
            ),
            Evidence(
                EvidenceKind.TRACKING_PRESENT,
                recorded.first_observed_at,
                f"tracking:{recorded.source.value}",
                str(recorded.id),
            ),
            actor=actor,
            correlation_id=correlation_id,
        )
        return recorded


@dataclass
class TrackingSync:
    """The `tracking-sync` handler."""

    recorder: TrackingRecorder
    store: TrackingStore
    shops: IntakeShops
    clock: Callable[[], datetime]
    offload: Offload = _inline
    batch: int = SCHEDULE_BATCH

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        if ctx.shop_id is None:
            raise JobError("a tracking sync names its shop")
        shop_id = ctx.shop_id
        integration = await self.shops.describe(shop_id)
        if (
            integration is None
            or integration.organization_id != ctx.organization_id
            or integration.status is not IntegrationStatus.CONNECTED
        ):
            return Completed({"skipped": "not_connected"})
        receipt_id = ctx.payload.get("receipt_id")
        if isinstance(receipt_id, int) and not isinstance(receipt_id, bool):
            receipts: tuple[int, ...] = (receipt_id,)
            via = ObservedVia.WEBHOOK
        elif ctx.payload.get("mode") == "schedule":
            receipts = await self.offload(
                lambda: self.store.untracked_shipped(shop_id, limit=self.batch)
            )
            via = ObservedVia.SCHEDULE
        else:
            raise JobError("a tracking sync names a receipt or the schedule mode")
        reader = GatewayReceipts(ctx, self.clock)
        recorded = 0
        for receipt in receipts:
            observed = await reader.get_order_receipt(receipt)
            recorded += await self._shipments(ctx, shop_id, observed, via)
        return Completed({"receipts": len(receipts), "recorded": recorded, "via": via.value})

    async def _shipments(
        self, ctx: HandlerContext, shop_id: uuid.UUID, receipt: OrderReceipt, via: ObservedVia
    ) -> int:
        created = 0
        for shipment in receipt.shipments:
            if not normalize_tracking_code(shipment.tracking_code):
                continue  # a shipment without a code: nothing to record yet
            result = await self.recorder.record(
                organization_id=ctx.organization_id,
                shop_id=shop_id,
                receipt_id=receipt.receipt_id,
                carrier_name=shipment.carrier,
                code=shipment.tracking_code,
                source=TrackingSource.ETSY,
                observed_via=via,
                notified_at=shipment.notified_at,
                actor=ActorRef(ActorKind.AUTOMATION, TRACKING_SYNC),
                execution_id=ctx.execution_id,
                correlation_id=ctx.correlation_id,
            )
            created += result.created
        return created


@dataclass
class EngineTrackingSync:
    """The `order.shipped` webhook's second trigger (beside Etsy Order Intake): one Tracking
    Synchronization per receipt and event. Refused (and logged) until the owner activates it."""

    engine: TriggersAutomations
    offload: Offload = _inline

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
            automation_id=TRACKING_SYNC,
            trigger=TriggerKind.EVENT,
            actor=WEBHOOK_ACTOR,
            idempotency_key=f"tracking:{shop.shop_id}:{receipt_id}:{event_type}",
            payload={"receipt_id": receipt_id, "event_type": event_type},
            correlation_id=correlation_id,
            organization_id=shop.organization_id,
            shop_id=shop.shop_id,
            causation_id=causation_id,
        )
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("fulfillment.tracking_not_started: %s shop=%s", event_type, shop.shop_id)
            return False
        return True
