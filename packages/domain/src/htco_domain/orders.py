"""Orders (FR-ORD-03, slice 5.4): the order state machine derived only from observable facts,
order and unit counts, and exact money.

`PAID → INGESTED → AWAITING_SHIPMENT → SHIPPED → DELIVERED`, plus `CANCELLED` and `REFUNDED`
(and `UNPAID` before the payment). `INGESTED` is HTCO's own fact: the order is stored. `SHIPPED`
means the marketplace has a shipment record. Each transition is recorded with its source and the
moment it was observed; a jump (an order first seen already shipped) is recorded as the legal
path through the states in between, so the history never skips the rules.
"""

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from htco_domain.state_machine import IllegalTransition, check_transition


class OrderState(StrEnum):
    UNPAID = "unpaid"
    PAID = "paid"
    INGESTED = "ingested"
    AWAITING_SHIPMENT = "awaiting_shipment"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"

    @property
    def is_final(self) -> bool:
        return self in {OrderState.CANCELLED, OrderState.REFUNDED}

    @property
    def counts_as_sale(self) -> bool:
        """Cancelled and fully refunded orders are not sales (AC-5.4.3)."""
        return self not in {OrderState.UNPAID, OrderState.CANCELLED, OrderState.REFUNDED}


_O = OrderState
ORDER_TRANSITIONS: Mapping[OrderState, frozenset[OrderState]] = {
    _O.UNPAID: frozenset({_O.PAID, _O.CANCELLED}),
    _O.PAID: frozenset({_O.INGESTED}),
    _O.INGESTED: frozenset({_O.AWAITING_SHIPMENT, _O.SHIPPED, _O.CANCELLED, _O.REFUNDED}),
    _O.AWAITING_SHIPMENT: frozenset({_O.SHIPPED, _O.CANCELLED, _O.REFUNDED}),
    _O.SHIPPED: frozenset({_O.DELIVERED, _O.REFUNDED}),
    _O.DELIVERED: frozenset({_O.REFUNDED}),
    _O.CANCELLED: frozenset(),
    _O.REFUNDED: frozenset(),
}
# Where an order's history starts: before or after its payment.
INITIAL_STATES = frozenset({_O.UNPAID, _O.PAID})


def check_order_transition(current: OrderState, target: OrderState) -> None:
    check_transition("order", ORDER_TRANSITIONS, current, target)


@dataclass(frozen=True)
class OrderFacts:
    """What the marketplace says about an order (no buyer data)."""

    status: str  # lower case: "paid", "completed", "open", "canceled", "fully refunded", ...
    is_paid: bool
    is_shipped: bool
    delivered: bool = False  # a delivery the marketplace announced, confirmed shipped


def observed_state(facts: OrderFacts) -> OrderState:
    """The state the facts show; `INGESTED` and `PAID` are never observed on their own."""
    status = facts.status.lower()
    if status in {"canceled", "cancelled"}:
        return _O.CANCELLED
    if status == "fully refunded":
        return _O.REFUNDED
    if facts.is_shipped or status == "completed":
        return _O.DELIVERED if facts.delivered else _O.SHIPPED
    if facts.is_paid:
        return _O.AWAITING_SHIPMENT
    return _O.UNPAID


def transition_path(current: OrderState | None, target: OrderState) -> tuple[OrderState, ...]:
    """The states to pass, in order, from `current` (None: not stored yet) to `target`; empty when
    already there. Raises `IllegalTransition` when `target` cannot be reached (it is behind)."""
    if current == target:
        return ()
    starts = [(state,) for state in INITIAL_STATES] if current is None else [(current,)]
    queue: deque[tuple[OrderState, ...]] = deque(starts)
    seen = {path[-1] for path in starts}
    while queue:
        path = queue.popleft()
        if path[-1] == target:
            return path if current is None else path[1:]
        for following in sorted(ORDER_TRANSITIONS[path[-1]], key=lambda s: s.value):
            if following not in seen:
                seen.add(following)
                queue.append((*path, following))
    raise IllegalTransition("order", current or _O.UNPAID, target)


def ingest_path(current: OrderState | None, facts: OrderFacts) -> tuple[OrderState, ...]:
    """The transitions an ingest records: a new paid order passes `PAID → INGESTED` first."""
    target = observed_state(facts)
    if current is None and target is _O.UNPAID:
        return (_O.UNPAID,)
    if current in {None, _O.UNPAID} and facts.is_paid:
        # Paid now: stored as soon as it is paid, then wherever the facts are.
        head = transition_path(current, _O.INGESTED)
        return head + transition_path(_O.INGESTED, target)
    return transition_path(current, target)


# --- Counts and money ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountedOrder:
    state: OrderState
    quantities: tuple[int, ...]  # one per item


def order_count(orders: Iterable[CountedOrder]) -> int:
    return sum(1 for order in orders if order.state.counts_as_sale)


def units_sold(orders: Iterable[CountedOrder]) -> int:
    return sum(sum(order.quantities) for order in orders if order.state.counts_as_sale)


@dataclass(frozen=True)
class Money:
    """An exact amount in a currency. Floats are refused (AC-5.4.4)."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount: object = self.amount
        if not isinstance(amount, Decimal):
            raise TypeError("money is a Decimal, never a float")
        if len(self.currency) != 3 or not self.currency.isupper():  # noqa: PLR2004 - ISO 4217
            raise ValueError("money currency is an ISO 4217 code such as USD")

    @classmethod
    def of(cls, amount: int, divisor: int, currency: str) -> Money:
        """`amount / divisor` exactly, as the marketplace sends it."""
        if isinstance(amount, float) or isinstance(divisor, float):
            raise TypeError("money is built from integers, never floats")
        if divisor <= 0:
            raise ValueError("money divisor must be positive")
        exponent = len(str(divisor)) - 1
        if divisor == 10**exponent:
            return cls(Decimal(amount).scaleb(-exponent), currency)
        return cls(Decimal(amount) / Decimal(divisor), currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)
