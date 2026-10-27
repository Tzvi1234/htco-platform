"""The "New product quotes" board and its notification (slice 7.16): totals in Decimal (a
property test), sorted by total, a quote past its validity greyed out and not choosable
(AC-7.16.2); Owner and Admins only; a new product's quote notifies the owner and admins once,
an alternative does not (AC-7.15.3). All values are invented."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_application.access.authentication import AuthContext, Membership
from htco_application.notifications import NotificationService
from htco_application.quote_board import (
    NotificationQuoteAlerts,
    QuoteRow,
    landed_total,
    quote_board,
    read_quote_board,
)
from htco_application.quote_mailbox import MemoryQuoteReviews
from htco_application.quote_parser import QuoteMail, QuoteParser
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_application.suppliers import IntakeOutcome, NewQuote, QuoteIntake, ShopForMail
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.notifications import NotificationKind
from htco_domain.suppliers import ProductRef
from tests.application.test_quote_mailbox import fixture, mail_of
from tests.application.test_supplier_quote_intake import ORG, SHOP, Store

__all__ = ["NOW", "SHOP", "Reader", "Shops", "auth", "row"]
from tests.notification_fakes import Member, MemoryNotificationStore

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
REQUEST = uuid.UUID("71600000-0000-0000-0000-000000000001")
USER = uuid.UUID("71600000-0000-0000-0000-0000000000aa")


def row(  # noqa: PLR0913 - a quote's facts
    price: str, shipping: str, *, currency: str = "USD", valid: date | None = None,
    request: uuid.UUID = REQUEST, supplier: str = "Supplier",
) -> QuoteRow:  # fmt: skip
    return QuoteRow(
        request_id=request, listing_id=1234567890, variant="", state="quotes_received",
        requested_at=NOW - timedelta(days=1), quote_id=uuid.uuid7(), supplier=supplier,
        currency=currency, unit_price=Decimal(price), shipping_cost=Decimal(shipping),
        shipping_method="Yun Express", processing_days=(2, 4), shipping_days=(7, 12),
        valid_until=valid, received_at=NOW,
    )  # fmt: skip


@pytest.mark.ac("AC-7.16.2")
@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal(99999), places=4),
    st.decimals(min_value=Decimal(0), max_value=Decimal(99999), places=4),
)
def test_total__is_the_exact_decimal_sum(price: Decimal, shipping: Decimal) -> None:
    [product] = quote_board([row(str(price), str(shipping))], today=TODAY)
    [card] = product.quotes
    assert card.total == landed_total(price, shipping) == price + shipping
    assert isinstance(card.total, Decimal)


@pytest.mark.ac("AC-7.16.2")
def test_quotes__sorted_by_total_with_an_expired_one_last_and_not_choosable() -> None:
    rows = [
        row("9.00", "0.00", supplier="B"),
        row("1.00", "0.50", supplier="Expired", valid=TODAY - timedelta(days=1)),
        row("7.40", "2.10", supplier="A"),
        row("7.00", "2.00", supplier="Valid today", valid=TODAY),
    ]
    [product] = quote_board(rows, today=TODAY)
    # A tie (9.00) keeps the order quotes arrived in; a quote valid until today is still valid.
    assert [(q.supplier, str(q.total), q.choosable) for q in product.quotes] == [
        ("B", "9.00", True),
        ("Valid today", "9.00", True),
        ("A", "9.50", True),
        ("Expired", "1.50", False),
    ]


def test_currencies__never_compared_with_each_other() -> None:
    [product] = quote_board(
        [row("5.00", "0", currency="USD"), row("3.00", "0", currency="EUR")], today=TODAY
    )
    assert [q.currency for q in product.quotes] == ["EUR", "USD"]


def test_a_request_without_quotes__a_card_with_none() -> None:
    waiting = QuoteRow(
        request_id=REQUEST, listing_id=1, variant="", state="waiting_for_quotes",
        requested_at=NOW,
    )  # fmt: skip
    [product] = quote_board([waiting], today=TODAY)
    assert product.quotes == ()


# --- Access -------------------------------------------------------------------------------------


@dataclass
class Reader:
    rows: list[QuoteRow] = field(default_factory=list)
    claims: list[Any] = field(default_factory=list)

    def open_quotes(self, claims: Any, shop_id: uuid.UUID) -> list[QuoteRow]:
        assert shop_id == SHOP
        self.claims.append(claims)
        return self.rows


class Shops:
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return ORG if shop_id == SHOP else None


def auth(role: Role) -> AuthContext:
    return AuthContext(
        user_id=USER, assurance=AssuranceLevel.AAL1, session_id=None,
        memberships=(Membership(ORG, role, frozenset({SHOP})),), claims={"sub": str(USER)},
    )  # fmt: skip


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
def test_board__owner_and_admin_read_it_as_themselves(role: Role) -> None:
    reader = Reader([row("7.40", "2.10")])
    board = read_quote_board(auth(role), reader, Shops(), SHOP, now=NOW)
    assert len(board) == 1
    assert reader.claims == [{"sub": str(USER)}]


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.FINANCE, Role.SUPPORT, Role.VIEWER])
def test_board__other_roles_are_refused(role: Role) -> None:
    with pytest.raises(ShopAccessDeniedError):
        read_quote_board(auth(role), Reader(), Shops(), SHOP, now=NOW)


def test_board__a_shop_the_caller_cannot_see_does_not_exist() -> None:
    with pytest.raises(ShopNotFoundError):
        read_quote_board(auth(Role.OWNER), Reader(), Shops(), uuid.uuid7(), now=NOW)


# --- The notification ---------------------------------------------------------------------------


@dataclass
class Alerts:
    sent: list[NewQuote] = field(default_factory=list)

    async def new_product_quote(self, quote: NewQuote) -> None:
        self.sent.append(quote)


def intake(store: Store, alerts: Alerts) -> QuoteIntake:
    return QuoteIntake(
        parser=QuoteParser("quotes@autova.example.com"), store=store,
        reviews=MemoryQuoteReviews(), clock=lambda: NOW,
        owner_mailbox="owner@shop.example.com", alerts=alerts,
    )  # fmt: skip


def first_mail() -> QuoteMail:
    return mail_of(fixture("01_en_usd.eml.txt"))


@pytest.mark.anyio
async def test_a_new_products_quote__alerts_with_a_stable_key() -> None:
    store, alerts = Store(), Alerts()
    target = ShopForMail(ORG, SHOP)
    assert (
        await intake(store, alerts).handle(first_mail(), target) is IntakeOutcome.NEW_PRODUCT_QUOTE
    )
    # A retried job: already recorded, alerted again with the same key, deduplicated downstream.
    assert (
        await intake(store, alerts).handle(first_mail(), target) is IntakeOutcome.ALREADY_RECORDED
    )
    first, again = alerts.sent
    assert first == again
    assert first.source_reference == "autova-quote:Q-88213"
    assert first.listing_id == 1234567890


@pytest.mark.anyio
@pytest.mark.ac("AC-7.15.3")
async def test_an_alternative__no_alert() -> None:
    store, alerts = Store(), Alerts()
    store.mapped.add(ProductRef(SHOP, 1234567890).key)
    outcome = await intake(store, alerts).handle(first_mail(), ShopForMail(ORG, SHOP))
    assert outcome is IntakeOutcome.ALTERNATIVE
    assert alerts.sent == []


@pytest.mark.anyio
async def test_notification__owner_and_admins_in_app_and_email_once() -> None:
    owner, admin, ops = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    store = MemoryNotificationStore(
        members=[
            Member(owner, "owner", "owner@shop.example.com"),
            Member(admin, "admin", "admin@shop.example.com"),
            Member(ops, "operations", "ops@shop.example.com"),
        ]
    )
    alerts = NotificationQuoteAlerts(NotificationService(store, clock=lambda: NOW))
    quote = NewQuote(ORG, SHOP, REQUEST, 1234567890, "autova-quote:Q-88213")
    await alerts.new_product_quote(quote)
    await alerts.new_product_quote(quote)

    notified = list(store.rows.values())
    assert {n.user_id for n in notified} == {owner, admin}
    assert {n.kind for n in notified} == {NotificationKind.NEW_PRODUCT_QUOTE}
    assert notified[0].params == {"listing_id": "1234567890", "quote_request_id": str(REQUEST)}
    assert len(store.outbox) == 2  # one email each, once
