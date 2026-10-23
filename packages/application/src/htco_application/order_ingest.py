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
from dataclasses import dataclass
from datetime import datetime
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
_BUYER_FIELDS = ("buyer_name", "shipping_address")


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


SYNC = IngestContext(IngestSource.SYNC)
BACKFILL = IngestContext(IngestSource.BACKFILL, publish=False)


@dataclass(frozen=True)
class StoredOrder:
    etsy_updated_at: datetime
    order_state: OrderState | None
    receipt_sha256: bytes | None


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


def plan_ingest(
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
    facts = OrderFacts(
        status=receipt.status,
        is_paid=receipt.is_paid,
        is_shipped=receipt.is_shipped,
        delivered=receipt.receipt_id in context.delivered,
    )
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
