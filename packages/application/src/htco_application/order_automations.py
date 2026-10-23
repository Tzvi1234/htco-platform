"""Order Deduplication & Verification and Product Sales Counters (slice 5.11), and what starts
them: the order events Etsy Order Intake publishes.

**Starting them.** Intake's ingest writes `orders.ingested.v1`, `orders.updated.v1`,
`orders.cancelled.v1` and `orders.refunded.v1` to the outbox (`events.publish`).
`OrderEventTriggers` delivers those events:

- an ingested or updated order starts **Order Deduplication & Verification** as a workflow step
  of Intake, keyed by the receipt and Etsy's update time;
- an ingested, updated, cancelled or refunded order starts **Product Sales Counters**, keyed by
  the receipt, the event and Etsy's update time.

An automation that is not active yet refuses the trigger. The refusal is logged and the event is
still delivered: nothing loops.

**Order Deduplication & Verification** re-reads the receipt through the Action Gateway
(`etsy.receipts.read`, GET-only) and compares it with the stored order: exactly one stored order
per receipt, the same grand total and the same item quantities. Each difference is one row in
`app.sync_drifts` (`missing`, `totals`, `items`; source `verification`) with an
`orders.drift_detected.v1` event. A copy older or newer than the stored one is not compared.

**Product Sales Counters** moves the per-listing counters by the difference between what the
receipt should count now (its items when the stored state counts as a sale, nothing otherwise)
and what it counted before, in one transaction. A replayed event changes nothing; a cancellation
takes the units back once.

No model call, no browser and no write to Etsy in either (AC-5.11.7).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import ORDER_INTAKE, TriggersAutomations
from htco_application.jobs import JobError, JsonValue
from htco_application.marketplace import OrderReceipt
from htco_application.messaging import OutboxMessage, PendingDelivery
from htco_application.order_ingest import (
    CANCELLED,
    DRIFT_DETECTED,
    EVENTS_DESTINATION,
    INGESTED,
    REFUNDED,
    UPDATED,
)
from htco_application.order_intake import GatewayReceipts, IntakeShops
from htco_application.shop_integrations import ShopIntegration
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.integrations import IntegrationStatus
from htco_domain.orders import OrderState

_log = logging.getLogger(__name__)

ORDER_DEDUP = "order-dedup-verification"
SALES_COUNTERS = "product-sales-counters"
EVENT_TRIGGER_ACTOR = ActorRef(ActorKind.SYSTEM, "order-events")
INTAKE_ACTOR = ActorRef(ActorKind.AUTOMATION, ORDER_INTAKE)
VERIFIED_EVENTS = frozenset({INGESTED, UPDATED})
COUNTED_EVENTS = frozenset({INGESTED, UPDATED, CANCELLED, REFUNDED})

type Offload = Callable[[Callable[[], Any]], Any]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


# --- Verification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredSnapshot:
    """What verification compares: the stored order's version, whether its grand total equals
    Etsy's (the worker never reads the amount), and its items."""

    etsy_updated_at: datetime
    total_matches: bool
    items: tuple[tuple[int, int], ...]  # (transaction_id, quantity), by transaction
    orders: int = 1  # stored orders for the receipt (the key allows one)


class DriftKinds:
    MISSING = "missing"
    TOTALS = "totals"
    ITEMS = "items"


@dataclass(frozen=True)
class Difference:
    kind: str
    stored: JsonValue
    observed: JsonValue


def verify(stored: StoredSnapshot | None, receipt: OrderReceipt) -> tuple[Difference, ...]:
    """The differences between the stored order and Etsy's copy of the same version. Item IDs
    and quantities only; a total that differs is named, never quoted (no money in drifts)."""
    if stored is None or stored.orders == 0:
        return (Difference(DriftKinds.MISSING, None, {"status": receipt.status}),)
    if stored.etsy_updated_at != receipt.updated_at:
        return ()  # another version: its own ingest and verification follow
    found: list[Difference] = []
    if not stored.total_matches:
        found.append(Difference(DriftKinds.TOTALS, {"grand_total": "stored"},
                                {"grand_total": "differs"}))  # fmt: skip
    seen = tuple(sorted((line.transaction_id, line.quantity) for line in receipt.lines))
    if tuple(sorted(stored.items)) != seen:
        found.append(
            Difference(
                DriftKinds.ITEMS,
                [list(item) for item in sorted(stored.items)],
                [list(item) for item in seen],
            )
        )
    return tuple(found)


def difference_event(
    integration: ShopIntegration, receipt: OrderReceipt, difference: Difference, correlation_id: str
) -> OutboxMessage:
    """`orders.drift_detected.v1` for a verification difference (no money, no buyer data)."""
    shop = integration.shop_id
    return OutboxMessage(
        organization_id=integration.organization_id,
        shop_id=shop,
        destination=EVENTS_DESTINATION,
        idempotency_key=(
            f"{DRIFT_DETECTED}:{shop}:{receipt.receipt_id}:{difference.kind}:"
            f"{int(receipt.updated_at.timestamp())}"
        )[:200],
        payload={
            "event_type": DRIFT_DETECTED,
            "shop_id": str(shop),
            "receipt_id": receipt.receipt_id,
            "kind": difference.kind,
            "stored": difference.stored,
            "observed": difference.observed,
            "etsy_updated_at": receipt.updated_at.isoformat(),
            "source": "verification",
        },
        correlation_id=correlation_id[:200],
    )


class VerificationStore(Protocol):
    async def snapshot(self, shop_id: Any, receipt: OrderReceipt) -> StoredSnapshot | None:
        """The stored order of the receipt, compared with this copy's grand total."""
        ...

    async def record_differences(  # noqa: PLR0913 - the order, what differed and the run
        self,
        integration: ShopIntegration,
        receipt: OrderReceipt,
        differences: tuple[Difference, ...],
        *,
        execution_id: Any,
        correlation_id: str,
        now: datetime,
    ) -> int:
        """One drift and one `orders.drift_detected.v1` per new difference; the number new."""
        ...


@dataclass
class OrderVerification:
    """The `order-dedup-verification` handler."""

    store: VerificationStore
    shops: IntakeShops
    clock: Callable[[], datetime]

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        receipt_id = ctx.payload.get("receipt_id")
        if ctx.shop_id is None or not isinstance(receipt_id, int) or isinstance(receipt_id, bool):
            raise JobError("a verification names its shop and receipt")
        integration = await self.shops.describe(ctx.shop_id)
        if (
            integration is None
            or integration.organization_id != ctx.organization_id
            or integration.status is not IntegrationStatus.CONNECTED
        ):
            return Completed({"skipped": "not_connected"})
        receipt = await GatewayReceipts(ctx, self.clock).get_order_receipt(receipt_id)
        stored = await self.store.snapshot(ctx.shop_id, receipt)
        differences = verify(stored, receipt)
        recorded = await self.store.record_differences(
            integration, receipt, differences, execution_id=ctx.execution_id,
            correlation_id=ctx.correlation_id, now=self.clock(),
        )  # fmt: skip
        return Completed(
            {
                "receipt_id": receipt_id,
                "differences": [d.kind for d in differences],
                "recorded": recorded,
            }
        )


# --- Counters -------------------------------------------------------------------------------


def desired_counts(
    state: OrderState | None, items: tuple[tuple[int | None, int], ...]
) -> dict[int, int]:
    """What a receipt should count now: its units per listing when it counts as a sale."""
    if state is None or not state.counts_as_sale:
        return {}
    counts: dict[int, int] = {}
    for listing_id, quantity in items:
        if listing_id is not None and quantity > 0:
            counts[listing_id] = counts.get(listing_id, 0) + quantity
    return counts


@dataclass(frozen=True)
class CounterDelta:
    """Per listing: units and orders to add (negative: take back)."""

    units: Mapping[int, int]
    orders: Mapping[int, int]

    @property
    def empty(self) -> bool:
        return not any(self.units.values()) and not any(self.orders.values())


def counter_delta(previous: Mapping[int, int], desired: Mapping[int, int]) -> CounterDelta:
    """The move from what the receipt counted to what it should count."""
    listings = set(previous) | set(desired)
    units = {lid: desired.get(lid, 0) - previous.get(lid, 0) for lid in listings}
    orders = {
        lid: (1 if desired.get(lid, 0) > 0 else 0) - (1 if previous.get(lid, 0) > 0 else 0)
        for lid in listings
    }
    return CounterDelta(
        units={k: v for k, v in units.items() if v},
        orders={k: v for k, v in orders.items() if v},
    )


class CountersStore(Protocol):
    async def recount(
        self, organization_id: Any, shop_id: Any, receipt_id: int, *, execution_id: Any,
        now: datetime,
    ) -> CounterDelta | None:  # fmt: skip
        """In one transaction: the stored order's state and items, the receipt's previous
        counts, the move (`counter_delta(previous, desired_counts(...))`) applied to the
        counters, and the new counts kept. None: no stored order."""
        ...


@dataclass
class ProductSalesCounters:
    """The `product-sales-counters` handler (no Etsy call)."""

    store: CountersStore
    clock: Callable[[], datetime]

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        receipt_id = ctx.payload.get("receipt_id")
        if ctx.shop_id is None or not isinstance(receipt_id, int) or isinstance(receipt_id, bool):
            raise JobError("a recount names its shop and receipt")
        delta = await self.store.recount(
            ctx.organization_id, ctx.shop_id, receipt_id, execution_id=ctx.execution_id,
            now=self.clock(),
        )  # fmt: skip
        if delta is None:
            return Completed({"receipt_id": receipt_id, "skipped": "not_stored"})
        return Completed(
            {
                "receipt_id": receipt_id,
                "units": {str(k): v for k, v in sorted(delta.units.items())},
                "orders": {str(k): v for k, v in sorted(delta.orders.items())},
            }
        )


# --- Starting them from the order events ----------------------------------------------------


@dataclass
class OrderEventTriggers:
    """The `events.publish` deliverer: order events start the two automations; every event is
    accepted (the outbox keeps it as the record)."""

    engine: TriggersAutomations
    offload: Offload = _inline

    async def deliver(self, delivery: PendingDelivery) -> None:
        """As an `events.publish` deliverer on its own."""
        await self.handle(delivery.message)

    async def handle(self, message: OutboxMessage) -> None:
        """As a subscriber of the shared `events.publish` deliverer (slice 6.4)."""
        payload = message.payload
        event_type = payload.get("event_type")
        receipt_id = payload.get("receipt_id")
        version = str(payload.get("etsy_updated_at") or "")
        if (
            not isinstance(event_type, str)
            or not isinstance(receipt_id, int)
            or message.shop_id is None
            or message.organization_id is None
        ):
            return
        cause = f"outbox:{message.idempotency_key}"[:200]
        correlation = message.correlation_id or cause
        if event_type in VERIFIED_EVENTS:
            await self._trigger(
                TriggerRequest(
                    automation_id=ORDER_DEDUP, trigger=TriggerKind.WORKFLOW, actor=INTAKE_ACTOR,
                    idempotency_key=f"verify:{message.shop_id}:{receipt_id}:{version}"[:200],
                    payload={"receipt_id": receipt_id}, correlation_id=correlation,
                    organization_id=message.organization_id, shop_id=message.shop_id,
                    causation_id=cause,
                )
            )  # fmt: skip
        if event_type in COUNTED_EVENTS:
            await self._trigger(
                TriggerRequest(
                    automation_id=SALES_COUNTERS, trigger=TriggerKind.EVENT,
                    actor=EVENT_TRIGGER_ACTOR,
                    idempotency_key=(
                        f"counters:{message.shop_id}:{receipt_id}:{event_type}:{version}"
                    )[:200],
                    payload={"receipt_id": receipt_id, "event_type": event_type},
                    correlation_id=correlation, organization_id=message.organization_id,
                    shop_id=message.shop_id, causation_id=cause,
                )
            )  # fmt: skip

    async def _trigger(self, request: TriggerRequest) -> None:
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("order_automation.not_started: %s", request.automation_id)


__all__ = [
    "COUNTED_EVENTS",
    "ORDER_DEDUP",
    "SALES_COUNTERS",
    "VERIFIED_EVENTS",
    "CounterDelta",
    "Difference",
    "OrderEventTriggers",
    "OrderVerification",
    "ProductSalesCounters",
    "StoredSnapshot",
    "counter_delta",
    "desired_counts",
    "verify",
]
