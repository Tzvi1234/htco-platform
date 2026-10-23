"""One order-state vocabulary (audit P0-1): the order store's state machine (FR-ORD-03,
`htco_domain.orders`) is the only classifier, and the dashboard's states are a view mapped from
it. Before, the dashboard tested `is_paid` before shipped and the store tested shipped first, so
an unpaid but shipped receipt was UNPAID on screen and SHIPPED in the store."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_application.order_ingest import receipt_facts
from htco_application.shop_dashboard import (
    ORDER_VIEW_STATES,
    OrderFilter,
    OrderViewState,
    is_sale,
    order_state,
)
from htco_domain.orders import OrderFacts, OrderState, observed_state
from tests.shop_data_fakes import NOW, receipt

STATUSES = [
    "open",
    "payment processing",
    "paid",
    "completed",
    "canceled",
    "fully refunded",
    "partially refunded",
]


def test_every_store_state__has_one_view_state() -> None:
    assert set(ORDER_VIEW_STATES) == set(OrderState)
    assert set(ORDER_VIEW_STATES.values()) == set(OrderViewState)


def test_view_states__are_the_orders_screen_filters() -> None:
    assert {s.value for s in OrderViewState} <= {f.value for f in OrderFilter}


@pytest.mark.parametrize(
    ("store", "view"),
    [
        (OrderState.UNPAID, OrderViewState.UNPAID),
        (OrderState.PAID, OrderViewState.PAID),
        (OrderState.INGESTED, OrderViewState.PAID),
        (OrderState.AWAITING_SHIPMENT, OrderViewState.PAID),
        (OrderState.SHIPPED, OrderViewState.SHIPPED),
        (OrderState.DELIVERED, OrderViewState.SHIPPED),
        (OrderState.CANCELLED, OrderViewState.CANCELED),
        (OrderState.REFUNDED, OrderViewState.REFUNDED),
    ],
)
def test_view_state__mapped_from_the_store_state(store: OrderState, view: OrderViewState) -> None:
    assert ORDER_VIEW_STATES[store] is view


@pytest.mark.parametrize(
    ("status", "paid", "shipped"), list(itertools.product(STATUSES, [False, True], [False, True]))
)
def test_dashboard_and_store__classify_every_receipt_the_same(
    status: str, paid: bool, shipped: bool
) -> None:
    found = receipt(1, created=NOW, status=status, paid=paid, shipped=shipped)
    stored = observed_state(OrderFacts(status=status, is_paid=paid, is_shipped=shipped))

    assert order_state(found) is ORDER_VIEW_STATES[stored]
    assert is_sale(found) is stored.counts_as_sale


def test_an_unpaid_but_shipped_receipt__shipped_on_the_screen_as_in_the_store() -> None:
    found = receipt(1, created=NOW, status="open", paid=False, shipped=True)

    assert observed_state(receipt_facts(found)) is OrderState.SHIPPED
    assert order_state(found) is OrderViewState.SHIPPED


@given(
    status=st.sampled_from(STATUSES),
    paid=st.booleans(),
    shipped=st.booleans(),
    delivered=st.booleans(),
)
def test_receipt_facts__what_the_ingest_and_the_dashboard_both_read(
    status: str, paid: bool, shipped: bool, delivered: bool
) -> None:
    found = receipt(1, created=NOW, status=status, paid=paid, shipped=shipped)

    facts = receipt_facts(found, delivered=delivered)

    assert facts == OrderFacts(status=status, is_paid=paid, is_shipped=shipped, delivered=delivered)
