"""What storing a receipt means (slice 5.4): replays and older copies change nothing, every
transition is recorded with its source, money is exact, events carry no buyer data."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from htco_application.marketplace import MarketplaceMoney, OrderReceipt
from htco_application.order_ingest import (
    BACKFILL,
    CANCELLED,
    INGESTED,
    REFUNDED,
    UPDATED,
    IngestContext,
    IngestPlan,
    IngestSource,
    StoredOrder,
    Transition,
    facts_hash,
    financial_entries,
    plan_ingest,
)
from htco_application.order_receipts import receipt_from_json, receipt_to_json
from htco_domain.orders import OrderState
from tests.order_intake_fakes import (
    FAKE_LINE,
    FAKE_NAME,
    FAKE_NOTE,
    FAKE_TRACKING,
    paid_receipt,
    refunded,
    shipped,
)

_O = OrderState
ORG = uuid.UUID("5a100000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("5a300000-0000-0000-0000-000000000001")
WEBHOOK = IngestContext(IngestSource.WEBHOOK, correlation_id="corr-ingest")


def plan(
    receipt: OrderReceipt, stored: StoredOrder | None = None, context: IngestContext = WEBHOOK
) -> IngestPlan:
    return plan_ingest(receipt, stored, context, organization_id=ORG, shop_id=SHOP)


def stored_after(receipt: OrderReceipt, p: IngestPlan) -> StoredOrder:
    return StoredOrder(receipt.updated_at, p.state, p.receipt_sha256)


@pytest.mark.ac("AC-5.4.5")
def test_a_new_paid_order__passes_paid_ingested_awaiting_with_one_ingested_event() -> None:
    receipt = paid_receipt()

    p = plan(receipt)

    assert p.write
    assert p.created
    assert p.state is _O.AWAITING_SHIPMENT
    assert p.transitions == (
        Transition(None, _O.PAID),
        Transition(_O.PAID, _O.INGESTED),
        Transition(_O.INGESTED, _O.AWAITING_SHIPMENT),
    )
    [event] = p.events
    assert event.payload["event_type"] == INGESTED
    assert event.idempotency_key == f"{INGESTED}:{SHOP}:{receipt.receipt_id}"
    assert event.destination == "events.publish"


@pytest.mark.ac("AC-5.4.1")
def test_the_same_receipt_replayed__writes_nothing_more() -> None:
    receipt = paid_receipt()
    first = plan(receipt)
    stored = stored_after(receipt, first)

    for _ in range(10):
        again = plan(receipt, stored)
        assert not again.write
        assert again.events == ()
        assert again.transitions == ()


@pytest.mark.ac("AC-5.4.2")
def test_an_older_copy_after_a_newer_one__changes_nothing() -> None:
    receipt = paid_receipt()
    newer = shipped(receipt, minutes=60)
    stored = stored_after(newer, plan(newer))

    older = plan(replace(receipt, updated_at=receipt.updated_at + timedelta(minutes=5)), stored)

    assert not older.write
    assert older.state is _O.SHIPPED


@pytest.mark.ac("AC-5.4.5")
def test_a_newer_copy__moves_the_state_and_publishes_an_update() -> None:
    receipt = paid_receipt()
    stored = stored_after(receipt, plan(receipt))
    newer = shipped(receipt, minutes=60)

    p = plan(newer, stored)

    assert p.transitions == (Transition(_O.AWAITING_SHIPMENT, _O.SHIPPED),)
    [event] = p.events
    assert event.payload["event_type"] == UPDATED
    assert event.payload["state"] == "shipped"


@pytest.mark.ac("AC-5.4.5")
def test_cancelled_and_refunded__publish_their_own_events() -> None:
    receipt = paid_receipt()
    stored = stored_after(receipt, plan(receipt))
    cancelled = replace(
        receipt, status="canceled", updated_at=receipt.updated_at + timedelta(hours=1)
    )
    gone = plan(cancelled, stored)
    assert {e.payload["event_type"] for e in gone.events} == {UPDATED, CANCELLED}

    back = refunded(receipt, minutes=90)
    money_back = plan(back, stored)
    assert {e.payload["event_type"] for e in money_back.events} == {UPDATED, REFUNDED}
    assert money_back.state is _O.REFUNDED


def test_a_state_behind_the_stored_one__is_written_without_a_transition() -> None:
    receipt = paid_receipt()
    later = shipped(receipt, minutes=60)
    stored = stored_after(later, plan(later))
    behind = replace(receipt, updated_at=later.updated_at + timedelta(minutes=1))

    p = plan(behind, stored)

    assert p.write
    assert p.behind
    assert p.transitions == ()
    assert p.state is _O.SHIPPED


def test_the_backfill__records_transitions_but_publishes_nothing() -> None:
    p = plan(paid_receipt(), context=BACKFILL)
    assert p.transitions
    assert p.events == ()


def test_delivered__only_when_announced_and_the_reread_shows_it_shipped() -> None:
    receipt = shipped(paid_receipt(), minutes=30)
    delivered = IngestContext(IngestSource.WEBHOOK, delivered=frozenset({receipt.receipt_id}))
    assert plan(receipt, context=delivered).state is _O.DELIVERED
    assert plan(paid_receipt(), context=delivered).state is _O.AWAITING_SHIPMENT


@pytest.mark.ac("AC-5.4.4")
def test_financial_entries__exact_decimals_with_their_currency() -> None:
    entries = financial_entries(refunded(paid_receipt(), minutes=5))
    assert [(e.kind, e.position, e.amount.amount, e.amount.currency) for e in entries] == [
        ("revenue", 0, Decimal("25.99"), "USD"),
        ("refund", 0, Decimal("-25.99"), "USD"),
    ]
    for entry in entries:
        assert isinstance(entry.amount.amount, Decimal)


@pytest.mark.ac("AC-5.4.6")
def test_the_hash_and_the_events_carry_no_buyer_data() -> None:
    receipt = refunded(shipped(paid_receipt(), minutes=5), minutes=10)
    p = plan(receipt)
    text = json.dumps([e.payload for e in p.events])
    for secret in (FAKE_NAME, FAKE_LINE, FAKE_TRACKING, FAKE_NOTE):
        assert secret not in text
    hidden = replace(
        receipt,
        buyer_name="Another Person",
        shipping_address=None,
        shipments=tuple(replace(s, tracking_code="OTHER") for s in receipt.shipments),
        refunds=tuple(replace(r, note_from_issuer="other") for r in receipt.refunds),
    )
    assert facts_hash(hidden) == facts_hash(receipt)  # personal data is outside the hash
    assert facts_hash(replace(receipt, status="paid")) != facts_hash(receipt)


@settings(max_examples=50)
@given(st.integers(1, 10**12), st.integers(0, 10**6), st.booleans(), st.booleans())
def test_receipt_json__round_trips(receipt_id: int, cents: int, paid: bool, ship: bool) -> None:
    receipt = paid_receipt(receipt_id, grand_total=MarketplaceMoney(cents, 100, "EUR"))
    if ship:
        receipt = shipped(receipt, minutes=3)
    receipt = replace(receipt, is_paid=paid)
    back = receipt_from_json(json.loads(json.dumps(receipt_to_json(receipt))))
    assert back == receipt


@pytest.mark.ac("AC-5.4.5")
def test_delivered_after_the_same_copy_was_stored__still_moves_to_delivered() -> None:
    """Codex review of #72: a sync stored the shipped receipt first; the later `order.delivered`
    re-read returns the identical copy, and the delivery must not be skipped."""
    receipt = shipped(paid_receipt(), minutes=30)
    stored = stored_after(receipt, plan(receipt))
    delivered = IngestContext(IngestSource.WEBHOOK, delivered=frozenset({receipt.receipt_id}))

    p = plan(receipt, stored, delivered)

    assert p.write
    assert p.transitions == (Transition(_O.SHIPPED, _O.DELIVERED),)
    assert p.state is _O.DELIVERED
    again = plan(receipt, stored_after(receipt, p), delivered)
    assert not again.write  # once delivered, a replay changes nothing
