"""What storing one receipt means (slice 5.4): the plan the order store carries out in one
transaction, shared by every path that writes an order (D-19, AC-5.4.6).

The #57 backfill and 30-day sync, Etsy Order Intake's webhook path and reconciliation all call the
store's ingest, which asks `plan_ingest` what to do with each receipt:

- **Older or identical copy:** nothing (Etsy's `updated_timestamp` is older than the stored one,
  or the order facts' SHA-256 is unchanged). A replay changes nothing (AC-5.4.1, AC-5.4.2).
- **New or newer:** the order with its items, refunds and buyer row is replaced; the order state
  moves along the machine (`htco_domain.orders`), every transition recorded with its source and
  `observed_at` (AC-5.4.5); the estimated revenue and refund lines are replaced (AC-5.4.4); and
  the events `orders.ingested.v1` (once per order), `orders.updated.v1`, `orders.cancelled.v1`
  and `orders.refunded.v1` go to the outbox, IDs and states only (no buyer data).

The hash covers order facts only: never the buyer's name, address, tracking numbers or the
seller's refund note.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from htco_application.jobs import JsonValue
from htco_application.marketplace import OrderReceipt
from htco_application.messaging import OutboxMessage
from htco_application.order_receipts import receipt_to_json
from htco_domain.orders import Money, OrderFacts, OrderState, ingest_path
from htco_domain.state_machine import IllegalTransition

EVENTS_DESTINATION = "events.publish"
INGESTED = "orders.ingested.v1"
UPDATED = "orders.updated.v1"
CANCELLED = "orders.cancelled.v1"
REFUNDED = "orders.refunded.v1"
DRIFT_DETECTED = "orders.drift_detected.v1"
# A drift is unique per (shop, receipt, kind, Etsy update time) in app.sync_drifts; its ID is
# derived from that key, so the event names the very row it recorded (slice 6.7).
_DRIFT_IDS = uuid.UUID("6f2e1c3a-7d4b-5e8f-9a0b-1c2d3e4f5a6b")
_BUYER_FIELDS = ("buyer_name", "shipping_address")


def receipt_facts(receipt: OrderReceipt, *, delivered: bool = False) -> OrderFacts:
    """What the order state machine reads from a receipt. The ingest and the dashboard both
    classify through it, so a receipt has one state everywhere (audit P0-1)."""
    return OrderFacts(
        status=receipt.status,
        is_paid=receipt.is_paid,
        is_shipped=receipt.is_shipped,
        delivered=delivered,
    )


class IngestSource(StrEnum):
    WEBHOOK = "webhook"
    RECONCILIATION = "reconciliation"
    BACKFILL = "backfill"
    SYNC = "sync"


@dataclass(frozen=True)
class IngestContext:
    source: IngestSource
    correlation_id: str | None = None
    execution_id: uuid.UUID | None = None
    # Receipts whose delivery the marketplace announced (`order.delivered`), confirmed shipped.
    delivered: frozenset[int] = frozenset()
    # The backfill writes years of history: its orders publish no events.
    publish: bool = True
    # Reconciliation (slice 5.5): compare what is stored with Etsy and record each difference.
    detect_drift: bool = False
    # A receipt this young may simply not have arrived yet: missing only when older.
    drift_grace: timedelta = timedelta(minutes=20)


SYNC = IngestContext(IngestSource.SYNC)
BACKFILL = IngestContext(IngestSource.BACKFILL, publish=False)


@dataclass(frozen=True)
class StoredFacts:
    """What reconciliation compares (slice 5.5): no money, no buyer data."""

    status: str
    is_shipped: bool
    items: tuple[tuple[int, int], ...]  # (transaction_id, quantity), in order
    shipments: tuple[tuple[str, str], ...]  # (carrier, notified_at ISO)


@dataclass(frozen=True)
class StoredOrder:
    etsy_updated_at: datetime
    order_state: OrderState | None
    receipt_sha256: bytes | None
    facts: StoredFacts | None = None  # read only when reconciliation compares


class DriftKind(StrEnum):
    MISSING = "missing"  # Etsy has it, HTCO did not
    STATUS = "status"
    ITEMS = "items"
    SHIPMENTS = "shipments"


@dataclass(frozen=True)
class Drift:
    kind: DriftKind
    stored: JsonValue
    observed: JsonValue


def observed_facts(receipt: OrderReceipt) -> StoredFacts:
    return StoredFacts(
        status=receipt.status,
        is_shipped=receipt.is_shipped,
        items=tuple((line.transaction_id, line.quantity) for line in receipt.lines),
        shipments=tuple((s.carrier, s.notified_at.isoformat()) for s in receipt.shipments),
    )


def _items(items: tuple[tuple[int, int], ...]) -> JsonValue:
    return [[transaction, quantity] for transaction, quantity in items]


def _shipments(shipments: tuple[tuple[str, str], ...]) -> JsonValue:
    return [[carrier, at] for carrier, at in shipments]


def detect_drifts(
    stored: StoredOrder | None, receipt: OrderReceipt, *, now: datetime, grace: timedelta
) -> tuple[Drift, ...]:
    """Each difference between the stored order and Etsy's copy (AC-5.5.4), with both values.
    A receipt HTCO never stored is missing once it is older than `grace`."""
    seen = observed_facts(receipt)
    if stored is None:
        if receipt.updated_at > now - grace:
            return ()
        return (Drift(DriftKind.MISSING, None, {"status": seen.status}),)
    facts = stored.facts
    if facts is None or stored.etsy_updated_at > receipt.updated_at:
        return ()  # nothing stored to compare, or HTCO is already ahead of this copy
    drifts = []
    if (facts.status, facts.is_shipped) != (seen.status, seen.is_shipped):
        drifts.append(
            Drift(
                DriftKind.STATUS,
                {"status": facts.status, "is_shipped": facts.is_shipped},
                {"status": seen.status, "is_shipped": seen.is_shipped},
            )
        )
    if facts.items != seen.items:
        drifts.append(Drift(DriftKind.ITEMS, _items(facts.items), _items(seen.items)))
    if facts.shipments != seen.shipments:
        drifts.append(
            Drift(DriftKind.SHIPMENTS, _shipments(facts.shipments), _shipments(seen.shipments))
        )
    return tuple(drifts)


@dataclass(frozen=True)
class FinancialEntry:
    kind: str  # "revenue" or "refund"
    position: int
    amount: Money
    occurred_at: datetime


@dataclass(frozen=True)
class Transition:
    from_state: OrderState | None
    to_state: OrderState


@dataclass(frozen=True)
class IngestPlan:
    write: bool
    created: bool
    state: OrderState | None
    transitions: tuple[Transition, ...]
    entries: tuple[FinancialEntry, ...]
    events: tuple[OutboxMessage, ...]
    receipt_sha256: bytes
    # The facts moved behind the stored state (a drift for reconciliation, never a transition).
    behind: bool = False
    # Differences reconciliation found (slice 5.5), recorded in `sync_drifts`.
    drifts: tuple[Drift, ...] = ()


def facts_hash(receipt: OrderReceipt) -> bytes:
    """SHA-256 of the order facts: no name, address, tracking number or refund note."""
    facts = {k: v for k, v in receipt_to_json(receipt).items() if k not in _BUYER_FIELDS}
    shipments = facts["shipments"]
    if isinstance(shipments, list):
        facts["shipments"] = [
            {k: v for k, v in s.items() if k != "tracking_code"}
            for s in shipments
            if isinstance(s, dict)
        ]
    refunds = facts["refunds"]
    if isinstance(refunds, list):
        facts["refunds"] = [
            {k: v for k, v in r.items() if k != "note_from_issuer"}
            for r in refunds
            if isinstance(r, dict)
        ]
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def financial_entries(receipt: OrderReceipt) -> tuple[FinancialEntry, ...]:
    """Estimated revenue (the grand total) and one negative line per refund (exact Decimals)."""
    total = receipt.grand_total
    entries = [
        FinancialEntry(
            "revenue",
            0,
            Money.of(total.amount, total.divisor, total.currency_code),
            receipt.created_at,
        )
    ]
    for position, refund in enumerate(receipt.refunds):
        amount = Money.of(refund.amount.amount, refund.amount.divisor, refund.amount.currency_code)
        entries.append(FinancialEntry("refund", position, -amount, refund.created_at))
    return tuple(entries)


def plan_ingest(  # noqa: PLR0913 - the receipt, what is stored, where and when
    receipt: OrderReceipt,
    stored: StoredOrder | None,
    context: IngestContext,
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    now: datetime | None = None,
) -> IngestPlan:
    plan = _plan(receipt, stored, context, organization_id=organization_id, shop_id=shop_id)
    if not context.detect_drift or now is None:
        return plan
    drifts = detect_drifts(stored, receipt, now=now, grace=context.drift_grace)
    if not drifts:
        return plan
    events = tuple(
        _drift_event(receipt, d, organization_id, shop_id, context, now=now) for d in drifts
    )
    return replace(plan, drifts=drifts, events=plan.events + events)


def drift_id(
    shop_id: uuid.UUID, receipt_id: int, kind: str, etsy_updated_at: datetime
) -> uuid.UUID:
    """The ID of the drift recorded for this receipt, kind and Etsy update time."""
    if etsy_updated_at.tzinfo is None:
        raise ValueError("a drift's Etsy update time needs a time zone")
    at = etsy_updated_at.astimezone(UTC).isoformat()
    return uuid.uuid5(_DRIFT_IDS, f"{shop_id}:{receipt_id}:{kind}:{at}")


def _drift_event(  # noqa: PLR0913 - one event's facts
    receipt: OrderReceipt,
    drift: Drift,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    context: IngestContext,
    *,
    now: datetime,
) -> OutboxMessage:
    """`orders.drift_detected.v1`: what differed and both values (no money, no buyer data), and
    the drift it recorded (its ID and when it was seen) for Incident Detection (AC-6.3.4)."""
    return OutboxMessage(
        organization_id=organization_id,
        shop_id=shop_id,
        destination=EVENTS_DESTINATION,
        idempotency_key=(
            f"{DRIFT_DETECTED}:{shop_id}:{receipt.receipt_id}:{drift.kind.value}:"
            f"{int(receipt.updated_at.timestamp())}"
        )[:200],
        payload={
            "event_type": DRIFT_DETECTED,
            "shop_id": str(shop_id),
            "receipt_id": receipt.receipt_id,
            "kind": drift.kind.value,
            "stored": drift.stored,
            "observed": drift.observed,
            "etsy_updated_at": receipt.updated_at.isoformat(),
            "drift_id": str(
                drift_id(shop_id, receipt.receipt_id, drift.kind.value, receipt.updated_at)
            ),
            "detected_at": now.isoformat(),
        },
        correlation_id=None if context.correlation_id is None else context.correlation_id[:200],
    )


def _plan(
    receipt: OrderReceipt,
    stored: StoredOrder | None,
    context: IngestContext,
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
) -> IngestPlan:
    digest = facts_hash(receipt)
    # A delivery is announced by the event, not the receipt (Etsy's copy can be identical to
    # the stored one): it still moves a shipped order on (Codex review of #72).
    newly_delivered = (
        stored is not None
        and receipt.receipt_id in context.delivered
        and stored.order_state is OrderState.SHIPPED
        and stored.etsy_updated_at <= receipt.updated_at
    )
    unchanged = (
        stored is not None
        and not newly_delivered
        and (
            stored.etsy_updated_at > receipt.updated_at
            or (stored.etsy_updated_at == receipt.updated_at and stored.receipt_sha256 == digest)
        )
    )
    if unchanged:
        assert stored is not None  # noqa: S101 - narrowed above
        return IngestPlan(False, False, stored.order_state, (), (), (), digest)
    current = None if stored is None else stored.order_state
    facts = receipt_facts(receipt, delivered=receipt.receipt_id in context.delivered)
    behind = False
    try:
        path = ingest_path(current, facts)
    except IllegalTransition:
        path, behind = (), True  # Etsy shows an earlier state: kept, reconciliation reports it
    states = [current, *path]
    transitions = tuple(Transition(a, b) for a, b in pairwise(states) if b is not None)
    state = path[-1] if path else current
    created = stored is None
    events = (
        _events(
            receipt,
            organization_id=organization_id,
            shop_id=shop_id,
            state=state,
            transitions=transitions,
            created=created,
            digest=digest,
            context=context,
        )
        if context.publish
        else ()
    )
    return IngestPlan(
        True, created, state, transitions, financial_entries(receipt), events, digest, behind
    )


def _events(  # noqa: PLR0913 - the order and what changed
    receipt: OrderReceipt,
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    state: OrderState | None,
    transitions: tuple[Transition, ...],
    created: bool,
    digest: bytes,
    context: IngestContext,
) -> tuple[OutboxMessage, ...]:
    base: dict[str, JsonValue] = {
        "shop_id": str(shop_id),
        "receipt_id": receipt.receipt_id,
        "state": None if state is None else state.value,
        "etsy_updated_at": receipt.updated_at.isoformat(),
        "source": context.source.value,
    }
    reached = {t.to_state for t in transitions}
    names: list[tuple[str, str]] = [
        (INGESTED, f"{INGESTED}:{shop_id}:{receipt.receipt_id}")
        if created
        else (UPDATED, f"{UPDATED}:{shop_id}:{receipt.receipt_id}:{digest.hex()[:32]}")
    ]
    if OrderState.CANCELLED in reached:
        names.append((CANCELLED, f"{CANCELLED}:{shop_id}:{receipt.receipt_id}"))
    if OrderState.REFUNDED in reached:
        names.append((REFUNDED, f"{REFUNDED}:{shop_id}:{receipt.receipt_id}"))
    return tuple(
        OutboxMessage(
            organization_id=organization_id,
            shop_id=shop_id,
            destination=EVENTS_DESTINATION,
            idempotency_key=key,
            payload={"event_type": name, **base},
            correlation_id=None if context.correlation_id is None else context.correlation_id[:200],
        )
        for name, key in names
    )
