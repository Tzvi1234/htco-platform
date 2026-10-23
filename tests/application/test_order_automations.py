"""Order Deduplication & Verification and Product Sales Counters (slice 5.11), and the order
events that start them (invented values only)."""

import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.jobs import JobError
from htco_application.messaging import OutboxMessage, PendingDelivery
from htco_application.order_automations import (
    ORDER_DEDUP,
    SALES_COUNTERS,
    CounterDelta,
    OrderEventTriggers,
    ProductSalesCounters,
    StoredSnapshot,
    counter_delta,
    desired_counts,
    verify,
)
from htco_application.order_ingest import CANCELLED, INGESTED, REFUNDED, UPDATED
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, ExecutionState, TriggerKind
from htco_domain.orders import OrderState
from tests.shop_data_fakes import NOW, line, receipt

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("05110000-0000-7000-8000-000000000001")
SHOP = uuid.UUID("05110000-0000-7000-8000-000000000002")


def stored_of(r: object, **changes: object) -> StoredSnapshot:
    found = receipt(1, created=NOW) if r is None else r
    values: dict[str, object] = {
        "etsy_updated_at": found.updated_at,  # type: ignore[attr-defined]
        "total_matches": True,
        "items": tuple((li.transaction_id, li.quantity) for li in found.lines),  # type: ignore[attr-defined]
    } | changes
    return StoredSnapshot(**values)  # type: ignore[arg-type]


# --- AC-5.11.4: verification ----------------------------------------------------------------


ETSY = receipt(7, created=NOW, lines=(line(70), line(71)))


@pytest.mark.ac("AC-5.11.4")
@pytest.mark.parametrize(
    ("stored", "kinds"),
    [
        (stored_of(ETSY), []),
        (None, ["missing"]),
        (stored_of(ETSY, orders=0), ["missing"]),
        (stored_of(ETSY, total_matches=False), ["totals"]),
        (stored_of(ETSY, items=((700, 1),)), ["items"]),
        (stored_of(ETSY, items=((700, 2), (710, 1))), ["items"]),
        (stored_of(ETSY, total_matches=False, items=()), ["totals", "items"]),
        # Another version of the order: its own ingest and verification follow.
        (stored_of(ETSY, etsy_updated_at=NOW - timedelta(hours=1), items=()), []),
    ],
    ids=["match", "not-stored", "no-order-row", "total", "fewer-items",
         "other-quantity", "both", "other-version"],
)  # fmt: skip
def test_verify__one_difference_per_kind_and_none_for_a_match(
    stored: StoredSnapshot | None, kinds: list[str]
) -> None:
    assert [d.kind for d in verify(stored, ETSY)] == kinds


def test_verify__a_total_difference__never_quotes_an_amount() -> None:
    [found] = verify(stored_of(ETSY, total_matches=False), ETSY)

    assert "2400" not in str(found)
    assert "24.00" not in str(found)


# --- AC-5.11.5: counters --------------------------------------------------------------------


@pytest.mark.ac("AC-5.11.5")
@pytest.mark.parametrize(
    "state",
    [OrderState.CANCELLED, OrderState.REFUNDED, OrderState.UNPAID, None],
)
def test_desired__cancelled_refunded_or_unpaid__counts_nothing(state: OrderState | None) -> None:
    assert desired_counts(state, ((11, 2), (12, 1))) == {}


def test_desired__a_sale__its_units_per_listing() -> None:
    assert desired_counts(OrderState.SHIPPED, ((11, 2), (12, 1), (11, 1), (None, 5))) == {
        11: 3,
        12: 1,
    }


STATES = st.sampled_from(list(OrderState))
ITEMS = st.lists(
    st.tuples(st.integers(min_value=1, max_value=4), st.integers(min_value=0, max_value=3)),
    max_size=4,
).map(tuple)


@dataclass
class Counters:
    units: Counter[int] = field(default_factory=Counter)
    orders: Counter[int] = field(default_factory=Counter)
    counted: dict[int, int] = field(default_factory=dict)

    def apply(self, state: OrderState, items: tuple[tuple[int, int], ...]) -> None:
        desired = desired_counts(state, items)
        delta = counter_delta(self.counted, desired)
        self.units.update(delta.units)
        self.orders.update(delta.orders)
        self.counted = desired


@pytest.mark.ac("AC-5.11.5")
@given(events=st.lists(st.tuples(STATES, ITEMS), min_size=1, max_size=8), replays=st.integers(1, 4))
def test_counters__any_sequence_and_any_replay__end_at_the_last_state(
    events: list[tuple[OrderState, tuple[tuple[int, int], ...]]], replays: int
) -> None:
    """However the events arrive and however often each is replayed, the counters hold what the
    receipt's last state counts, never a negative number, and a cancellation once."""
    counters = Counters()
    for state, items in events:
        for _ in range(replays):
            counters.apply(state, items)
        assert all(v >= 0 for v in counters.units.values())
        assert all(v in {0, 1} for v in counters.orders.values())

    last_state, last_items = events[-1]
    expected = desired_counts(last_state, last_items)
    assert {k: v for k, v in counters.units.items() if v} == expected
    assert {k for k, v in counters.orders.items() if v} == set(expected)


@pytest.mark.ac("AC-5.11.5")
def test_counters__a_replayed_cancellation__takes_the_units_back_once() -> None:
    counters = Counters()
    counters.apply(OrderState.AWAITING_SHIPMENT, ((11, 2),))
    for _ in range(3):
        counters.apply(OrderState.CANCELLED, ((11, 2),))

    assert counters.units[11] == 0
    assert counters.orders[11] == 0


# --- the rebuild: receipts stored without an event (the history backfill, the draft period) --


@dataclass
class LedgerStore:
    """A `CountersStore` over invented stored orders, with the applications ledger."""

    orders: dict[int, tuple[OrderState, tuple[tuple[int, int], ...]]]
    counters: Counters = field(default_factory=Counters)
    applied: dict[int, dict[int, int]] = field(default_factory=dict)
    pages: list[tuple[int, int]] = field(default_factory=list)

    async def recount(
        self, organization_id: object, shop_id: object, receipt_id: int, *,  # noqa: ARG002
        execution_id: object, now: object,  # noqa: ARG002 - the CountersStore protocol
    ) -> CounterDelta | None:  # fmt: skip
        if receipt_id not in self.orders:
            return None
        state, items = self.orders[receipt_id]
        desired = desired_counts(state, items)
        delta = counter_delta(self.applied.get(receipt_id, {}), desired)
        self.counters.units.update(delta.units)
        self.counters.orders.update(delta.orders)
        self.applied[receipt_id] = desired
        return delta

    async def stored_receipts(
        self,
        shop_id: object,  # noqa: ARG002 - the protocol
        *,
        after: int,
        limit: int,
    ) -> list[int]:
        self.pages.append((after, limit))
        return sorted(r for r in self.orders if r > after)[:limit]


def rebuild_context(payload: dict[str, object]) -> HandlerContext:
    return HandlerContext(
        execution_id=uuid.uuid7(), automation_id=SALES_COUNTERS,
        actor=ActorRef(ActorKind.USER, "05110000-0000-7000-8000-000000000009"),
        organization_id=ORG, shop_id=SHOP, correlation_id="corr-rebuild", causation_id=None,
        attempt=1, payload=payload, gateway=None,  # type: ignore[arg-type]
    )  # fmt: skip


BACKFILLED = {
    101: (OrderState.AWAITING_SHIPMENT, ((7, 2),)),
    102: (OrderState.DELIVERED, ((7, 1), (8, 3))),
    103: (OrderState.CANCELLED, ((8, 5),)),
}


@pytest.mark.ac("AC-5.11.5")
async def test_rebuild__receipts_stored_by_the_backfill__counted_once_however_often_run() -> None:
    """The history backfill publishes no events, so its receipts are counted only by the
    rebuild; running it again (or after the events) moves nothing."""
    store = LedgerStore(dict(BACKFILLED))
    handler = ProductSalesCounters(store, clock=lambda: NOW, page_size=2)

    first = await handler(rebuild_context({"rebuild": True}))
    again = await handler(rebuild_context({"rebuild": True}))

    assert isinstance(first, Completed)
    assert first.output == {"rebuilt": 3, "moved": 2}
    assert isinstance(again, Completed)
    assert again.output == {"rebuilt": 3, "moved": 0}
    assert dict(store.counters.units) == {7: 3, 8: 3}
    assert dict(store.counters.orders) == {7: 2, 8: 1}
    assert store.pages[:3] == [(0, 2), (102, 2), (103, 2)]  # keyset pages, never one huge read


@pytest.mark.ac("AC-5.11.5")
async def test_rebuild__after_the_events_counted_a_receipt__moves_only_the_rest() -> None:
    store = LedgerStore(dict(BACKFILLED))
    handler = ProductSalesCounters(store, clock=lambda: NOW)
    await handler(rebuild_context({"receipt_id": 101, "event_type": INGESTED}))

    result = await handler(rebuild_context({"rebuild": True}))

    assert isinstance(result, Completed)
    assert result.output == {"rebuilt": 3, "moved": 1}
    assert dict(store.counters.units) == {7: 3, 8: 3}


@pytest.mark.parametrize(
    "payload", [{}, {"rebuild": False}, {"rebuild": True, "receipt_id": 101}, {"rebuild": "yes"}]
)
async def test_rebuild__a_malformed_request__refused(payload: dict[str, object]) -> None:
    handler = ProductSalesCounters(LedgerStore(dict(BACKFILLED)), clock=lambda: NOW)

    with pytest.raises(JobError):
        await handler(rebuild_context(payload))


# --- the events that start them -------------------------------------------------------------


@dataclass
class Engine:
    refuse: frozenset[str] = frozenset()
    requests: list[TriggerRequest] = field(default_factory=list)

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        self.requests.append(request)
        if request.automation_id in self.refuse:
            raise AutomationRefused(f"{request.automation_id} is not active (draft)")
        return AutomationExecution(
            id=uuid.uuid7(), automation_id=request.automation_id, automation_version=1,
            idempotency_key=request.idempotency_key, trigger=request.trigger,
            actor=request.actor, state=ExecutionState.PENDING, attempts=0,
            correlation_id=request.correlation_id, organization_id=request.organization_id,
            shop_id=request.shop_id,
        )  # fmt: skip


def event(
    event_type: str, receipt_id: int = 7, updated: str = "2026-10-23T10:00:00+00:00"
) -> PendingDelivery:
    return PendingDelivery(
        id=uuid.uuid4(),
        message=OutboxMessage(
            organization_id=ORG, shop_id=SHOP, destination="events.publish",
            idempotency_key=f"{event_type}:{receipt_id}",
            payload={"event_type": event_type, "receipt_id": receipt_id,
                     "etsy_updated_at": updated, "state": "paid"},
            correlation_id="corr-ev",
        ),
        attempts=0,
        delivered=False,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("event_type", "started"),
    [
        (INGESTED, [ORDER_DEDUP, SALES_COUNTERS]),
        (UPDATED, [ORDER_DEDUP, SALES_COUNTERS]),
        (CANCELLED, [SALES_COUNTERS]),
        (REFUNDED, [SALES_COUNTERS]),
        ("orders.drift_detected.v1", []),
        ("actions.executed.v1", []),
    ],
)
async def test_events__start_the_order_automations(event_type: str, started: list[str]) -> None:
    engine = Engine()

    await OrderEventTriggers(engine).deliver(event(event_type))

    assert [r.automation_id for r in engine.requests] == started
    for request in engine.requests:
        assert (request.organization_id, request.shop_id) == (ORG, SHOP)
        assert request.payload["receipt_id"] == 7
        assert request.correlation_id == "corr-ev"


async def test_events__verification_is_a_workflow_step_of_intake() -> None:
    engine = Engine()

    await OrderEventTriggers(engine).deliver(event(INGESTED))

    verification = next(r for r in engine.requests if r.automation_id == ORDER_DEDUP)
    assert verification.trigger is TriggerKind.WORKFLOW
    assert verification.actor.id == "etsy-order-intake"


async def test_events__the_same_version_twice__the_same_keys() -> None:
    engine = Engine()
    triggers = OrderEventTriggers(engine)

    await triggers.deliver(event(INGESTED))
    await triggers.deliver(event(INGESTED))
    await triggers.deliver(event(UPDATED, updated="2026-10-23T11:00:00+00:00"))

    keys = Counter(r.idempotency_key for r in engine.requests)
    assert sorted(keys.values()) == [1, 1, 2, 2]


async def test_events__an_automation_not_active_yet__refused_logged_and_delivered() -> None:
    engine = Engine(refuse=frozenset({ORDER_DEDUP, SALES_COUNTERS}))

    await OrderEventTriggers(engine).deliver(event(INGESTED))  # no exception: delivered

    assert len(engine.requests) == 2


async def test_events__without_a_shop__nothing_started() -> None:
    engine = Engine()
    delivery = event(INGESTED)
    delivery = replace(delivery, message=replace(delivery.message, shop_id=None))

    await OrderEventTriggers(engine).deliver(delivery)

    assert engine.requests == []
