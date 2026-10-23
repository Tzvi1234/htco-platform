"""The order state machine, counts and money (FR-ORD-03, slice 5.4)."""

from decimal import Decimal
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_domain.orders import (
    INITIAL_STATES,
    ORDER_TRANSITIONS,
    CountedOrder,
    Money,
    OrderFacts,
    OrderState,
    check_order_transition,
    ingest_path,
    observed_state,
    order_count,
    transition_path,
    units_sold,
)
from htco_domain.state_machine import IllegalTransition

_O = OrderState
ALLOWED = [(a, b) for a, targets in ORDER_TRANSITIONS.items() for b in sorted(targets)]
ILLEGAL = [(a, b) for a, b in product(OrderState, OrderState) if b not in ORDER_TRANSITIONS[a]]


@pytest.mark.ac("AC-5.4.5")
@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_transition__every_allowed_edge_passes(current: OrderState, target: OrderState) -> None:
    check_order_transition(current, target)


@pytest.mark.ac("AC-5.4.5")
@pytest.mark.parametrize(("current", "target"), ILLEGAL)
def test_transition__every_other_edge_is_refused(current: OrderState, target: OrderState) -> None:
    with pytest.raises(IllegalTransition):
        check_order_transition(current, target)


def test_machine__the_documented_order_and_final_states() -> None:
    assert ORDER_TRANSITIONS[_O.PAID] == {_O.INGESTED}
    assert ORDER_TRANSITIONS[_O.CANCELLED] == frozenset()
    assert ORDER_TRANSITIONS[_O.REFUNDED] == frozenset()
    assert {_O.UNPAID, _O.PAID} == INITIAL_STATES


@pytest.mark.parametrize(
    ("facts", "state"),
    [
        (OrderFacts("open", is_paid=False, is_shipped=False), _O.UNPAID),
        (OrderFacts("paid", is_paid=True, is_shipped=False), _O.AWAITING_SHIPMENT),
        (OrderFacts("completed", is_paid=True, is_shipped=True), _O.SHIPPED),
        (OrderFacts("completed", is_paid=True, is_shipped=True, delivered=True), _O.DELIVERED),
        (OrderFacts("paid", is_paid=True, is_shipped=False, delivered=True), _O.AWAITING_SHIPMENT),
        (OrderFacts("canceled", is_paid=True, is_shipped=False), _O.CANCELLED),
        (OrderFacts("fully refunded", is_paid=True, is_shipped=True), _O.REFUNDED),
        (OrderFacts("partially refunded", is_paid=True, is_shipped=True), _O.SHIPPED),
    ],
)
def test_observed_state__from_the_marketplace_facts_only(
    facts: OrderFacts, state: OrderState
) -> None:
    assert observed_state(facts) is state


@pytest.mark.ac("AC-5.4.5")
@pytest.mark.parametrize(
    ("current", "facts", "path"),
    [
        (None, OrderFacts("paid", True, False), (_O.PAID, _O.INGESTED, _O.AWAITING_SHIPMENT)),
        (None, OrderFacts("completed", True, True), (_O.PAID, _O.INGESTED, _O.SHIPPED)),
        (None, OrderFacts("open", False, False), (_O.UNPAID,)),
        (None, OrderFacts("canceled", True, False), (_O.PAID, _O.INGESTED, _O.CANCELLED)),
        (None, OrderFacts("canceled", False, False), (_O.UNPAID, _O.CANCELLED)),
        (_O.UNPAID, OrderFacts("paid", True, False), (_O.PAID, _O.INGESTED, _O.AWAITING_SHIPMENT)),
        (_O.AWAITING_SHIPMENT, OrderFacts("completed", True, True), (_O.SHIPPED,)),
        (_O.SHIPPED, OrderFacts("completed", True, True, delivered=True), (_O.DELIVERED,)),
        (_O.DELIVERED, OrderFacts("fully refunded", True, True), (_O.REFUNDED,)),
        (_O.SHIPPED, OrderFacts("completed", True, True), ()),
    ],
)
def test_ingest_path__passes_every_state_in_between(
    current: OrderState | None, facts: OrderFacts, path: tuple[OrderState, ...]
) -> None:
    assert ingest_path(current, facts) == path


@pytest.mark.ac("AC-5.4.5")
@pytest.mark.parametrize(
    ("current", "target"),
    [(_O.SHIPPED, _O.AWAITING_SHIPMENT), (_O.CANCELLED, _O.SHIPPED), (_O.REFUNDED, _O.PAID)],
)
def test_transition_path__a_state_behind_is_refused(
    current: OrderState, target: OrderState
) -> None:
    with pytest.raises(IllegalTransition):
        transition_path(current, target)


@given(st.sampled_from(list(OrderState)), st.sampled_from(list(OrderState)))
def test_transition_path__every_step_is_a_legal_edge(
    current: OrderState, target: OrderState
) -> None:
    try:
        path = transition_path(current, target)
    except IllegalTransition:
        return
    for a, b in zip((current, *path), path, strict=False):
        check_order_transition(a, b)
    assert not path or path[-1] is target


# --- Counts (AC-5.4.3) ---------------------------------------------------------------------

orders = st.lists(
    st.builds(
        CountedOrder,
        state=st.sampled_from(list(OrderState)),
        quantities=st.lists(st.integers(0, 50), max_size=5).map(tuple),
    ),
    max_size=30,
)


@pytest.mark.ac("AC-5.4.3")
@given(orders)
def test_counts__cancelled_refunded_and_unpaid_orders_never_count(
    batch: list[CountedOrder],
) -> None:
    sales = [o for o in batch if o.state not in {_O.CANCELLED, _O.REFUNDED, _O.UNPAID}]
    assert order_count(batch) == len(sales)
    assert units_sold(batch) == sum(sum(o.quantities) for o in sales)


@pytest.mark.ac("AC-5.4.3")
@given(orders, st.sampled_from([_O.CANCELLED, _O.REFUNDED]))
def test_counts__units_and_orders_are_counted_separately(
    batch: list[CountedOrder], excluded: OrderState
) -> None:
    extra = CountedOrder(excluded, (7, 3))
    assert order_count([*batch, extra]) == order_count(batch)
    assert units_sold([*batch, extra]) == units_sold(batch)
    many = CountedOrder(_O.SHIPPED, (2, 3))
    assert order_count([*batch, many]) == order_count(batch) + 1
    assert units_sold([*batch, many]) == units_sold(batch) + 5


# --- Money (AC-5.4.4) -------------------------------------------------------------------------


@pytest.mark.ac("AC-5.4.4")
def test_money__a_float_anywhere_fails() -> None:
    with pytest.raises(TypeError):
        Money(25.99, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money.of(25.99, 100, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money.of(2599, 100.0, "USD")  # type: ignore[arg-type]


@pytest.mark.ac("AC-5.4.4")
@given(st.integers(-(10**12), 10**12), st.sampled_from([1, 10, 100, 1000]))
def test_money__exact_from_amount_and_divisor(amount: int, divisor: int) -> None:
    money = Money.of(amount, divisor, "EUR")
    assert isinstance(money.amount, Decimal)
    assert money.amount * divisor == Decimal(amount)


def test_money__keeps_the_scale_and_negates() -> None:
    assert Money.of(2599, 100, "USD").amount == Decimal("25.99")
    assert str(Money.of(2500, 100, "USD").amount) == "25.00"
    assert (-Money.of(500, 100, "USD")).amount == Decimal("-5.00")
    with pytest.raises(ValueError, match="currency"):
        Money(Decimal(1), "usd")
    with pytest.raises(ValueError, match="divisor"):
        Money.of(1, 0, "USD")
