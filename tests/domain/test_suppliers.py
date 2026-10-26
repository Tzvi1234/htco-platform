"""Suppliers, quote requests, supplier quotes and supplier mappings (slice 7.13).

A product (a listing, or one variant of it) waits for a supplier through one quote request; its
quotes are money in a currency, never a float; and it has at most one active supplier mapping,
so activating a new one closes the old one with a reason (AC-7.13.1, AC-7.13.2)."""

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from htco_domain.orders import Money
from htco_domain.state_machine import IllegalTransition
from htco_domain.suppliers import (
    QUOTE_REQUEST_TRANSITIONS,
    MappingEndReason,
    ProductRef,
    QuoteRequest,
    QuoteRequestState,
    Supplier,
    SupplierMapping,
    SupplierQuote,
    activate_mapping,
)

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ORG = uuid.UUID("71300000-0000-0000-0000-000000000001")
SHOP = uuid.UUID("71300000-0000-0000-0000-000000000002")
PRODUCT = ProductRef(shop_id=SHOP, listing_id=1_234_567_890)


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def quote(**changes: Any) -> SupplierQuote:
    values: dict[str, Any] = {
        "id": uuid.uuid7(),
        "organization_id": ORG,
        "quote_request_id": uuid.uuid7(),
        "supplier_id": uuid.uuid7(),
        "unit_price": usd("7.40"),
        "shipping_cost": usd("2.10"),
        "shipping_method": "Yun Express",
        "processing_days": (2, 4),
        "shipping_days": (7, 12),
        "valid_until": date(2026, 11, 10),
        "source_reference": "autova-mail:4f1c2a",
        "received_at": NOW,
    } | changes
    return SupplierQuote(**values)


def mapping(supplier: uuid.UUID | None = None, **changes: Any) -> SupplierMapping:
    values: dict[str, Any] = {
        "id": uuid.uuid7(),
        "organization_id": ORG,
        "product": PRODUCT,
        "supplier_id": supplier or uuid.uuid7(),
        "quote_id": uuid.uuid7(),
        "active_from": NOW,
    } | changes
    return SupplierMapping(**values)


# --- Money (AC-7.13.2) --------------------------------------------------------------------------


def test_quote__landed_cost_is_the_unit_price_plus_shipping_in_decimal() -> None:
    assert quote().landed_cost == usd("9.50")


@pytest.mark.ac("AC-7.13.2")
@pytest.mark.parametrize("field", ["unit_price", "shipping_cost"])
def test_quote__without_a_currency__is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="ISO 4217"):
        quote(**{field: Money(Decimal("7.40"), "")})


@pytest.mark.ac("AC-7.13.2")
def test_quote__a_float_price__is_rejected() -> None:
    with pytest.raises(TypeError, match="never a float"):
        quote(unit_price=Money(7.4, "USD"))  # type: ignore[arg-type]


def test_quote__the_price_and_the_shipping_share_one_currency() -> None:
    with pytest.raises(ValueError, match="one currency"):
        quote(shipping_cost=Money(Decimal("2.10"), "EUR"))


@pytest.mark.parametrize(
    "changes",
    [
        {"unit_price": usd("0")},
        {"unit_price": usd("-1")},
        {"shipping_cost": usd("-0.01")},
        {"processing_days": (4, 2)},
        {"shipping_days": (-1, 3)},
        {"shipping_days": (1, 400)},
        {"shipping_method": ""},
        {"source_reference": "x" * 201},
        {"received_at": datetime(2026, 10, 27, 9, 0)},  # noqa: DTZ001 - a naive time is refused
    ],
)
def test_quote__invalid_values__are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r".+"):
        quote(**changes)


def test_quote__free_shipping_is_fine() -> None:
    assert quote(shipping_cost=usd("0")).landed_cost == usd("7.40")


def test_quote__valid_until_a_day__expires_after_it() -> None:
    q = quote(valid_until=date(2026, 10, 27))
    assert q.is_valid_on(date(2026, 10, 27))
    assert not q.is_valid_on(date(2026, 10, 28))
    assert quote(valid_until=None).is_valid_on(date(2030, 1, 1))


# --- The quote request's machine ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "target"),
    [(s, t) for s in QuoteRequestState for t in QuoteRequestState],
)
def test_quote_request__only_the_machines_edges_are_allowed(
    state: QuoteRequestState, target: QuoteRequestState
) -> None:
    request = QuoteRequest(uuid.uuid7(), ORG, PRODUCT, state, NOW)
    if target in QUOTE_REQUEST_TRANSITIONS[state]:
        assert request.transition(target, at=NOW).state is target
    else:
        with pytest.raises(IllegalTransition):
            request.transition(target, at=NOW)


def test_quote_request__the_happy_path_from_waiting_to_mapped() -> None:
    request = QuoteRequest(uuid.uuid7(), ORG, PRODUCT, QuoteRequestState.WAITING_FOR_QUOTES, NOW)
    for state in (
        QuoteRequestState.QUOTES_RECEIVED,
        QuoteRequestState.CHOSEN,
        QuoteRequestState.CONFIRMED_IN_AUTOVA,
        QuoteRequestState.MAPPED,
    ):
        request = request.transition(state, at=NOW)
    assert request.state is QuoteRequestState.MAPPED
    assert request.is_final


def test_quote_request__a_rejected_choice_goes_back_to_the_quotes() -> None:
    """The owner's approval of a choice is rejected or expires (7.17): he chooses again."""
    request = QuoteRequest(uuid.uuid7(), ORG, PRODUCT, QuoteRequestState.CHOSEN, NOW)
    assert request.transition(QuoteRequestState.QUOTES_RECEIVED, at=NOW).state is (
        QuoteRequestState.QUOTES_RECEIVED
    )


def test_quote_request__mapped_and_cancelled_are_final() -> None:
    for state in (QuoteRequestState.MAPPED, QuoteRequestState.CANCELLED):
        assert QUOTE_REQUEST_TRANSITIONS[state] == frozenset()


def test_product__a_listing_or_one_variant_of_it() -> None:
    assert PRODUCT.key == f"{SHOP}:1234567890:"
    assert ProductRef(SHOP, 1_234_567_890, variant="pid:42").key == f"{SHOP}:1234567890:pid:42"
    with pytest.raises(ValueError, match="listing"):
        ProductRef(SHOP, 0)
    with pytest.raises(ValueError, match="variant"):
        ProductRef(SHOP, 1, variant="x" * 121)


def test_supplier__a_name_and_how_autova_shows_it() -> None:
    supplier = Supplier(uuid.uuid7(), ORG, "Guangzhou Leather Co.", autova_name="GZ Leather")
    assert supplier.autova_name == "GZ Leather"
    with pytest.raises(ValueError, match="name"):
        Supplier(uuid.uuid7(), ORG, " ")


# --- One active mapping (AC-7.13.1) -------------------------------------------------------------


@pytest.mark.ac("AC-7.13.1")
def test_mapping__the_first_one_activates_alone() -> None:
    new = mapping()
    closed, active = activate_mapping(None, new, at=NOW)
    assert closed is None
    assert active == new
    assert active.is_active


@pytest.mark.ac("AC-7.13.1")
def test_mapping__a_new_one_closes_the_old_one_with_a_reason() -> None:
    old = mapping(active_from=NOW - timedelta(days=30))
    new = mapping()

    closed, active = activate_mapping(old, new, at=NOW, reason=MappingEndReason.REPLACED)

    assert closed is not None
    assert (closed.id, closed.ended_at, closed.end_reason) == (
        old.id,
        NOW,
        MappingEndReason.REPLACED,
    )
    assert not closed.is_active
    assert active.is_active


@pytest.mark.ac("AC-7.13.1")
def test_mapping__another_products_mapping_is_never_closed() -> None:
    other = mapping(product=ProductRef(SHOP, 999))
    with pytest.raises(ValueError, match="same product"):
        activate_mapping(other, mapping(), at=NOW)


@pytest.mark.ac("AC-7.13.1")
def test_mapping__an_ended_one_cannot_be_ended_again_or_activated() -> None:
    ended = replace(mapping(), ended_at=NOW, end_reason=MappingEndReason.REPLACED)
    with pytest.raises(ValueError, match="already ended"):
        activate_mapping(ended, mapping(), at=NOW)
    with pytest.raises(ValueError, match="active"):
        activate_mapping(None, ended, at=NOW)


def test_mapping__an_end_needs_its_reason_and_the_other_way_round() -> None:
    with pytest.raises(ValueError, match="reason"):
        mapping(ended_at=NOW)
    with pytest.raises(ValueError, match="reason"):
        mapping(end_reason=MappingEndReason.REPLACED)
    with pytest.raises(ValueError, match="before"):
        mapping(ended_at=NOW - timedelta(days=1), end_reason=MappingEndReason.REPLACED)
