"""The quote intake's store on real Postgres (slices 7.13, 7.15), as the worker in its job's
organization: the first quote creates the supplier and the product's quote request, the same quote
mail twice stores one quote, a second supplier's quote joins the same request, and a mapped
product's quote is an alternative."""

import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest

from htco_application.quote_parser import QuoteMail, QuoteParser
from htco_application.suppliers import IntakeOutcome, QuoteIntake, ShopForMail
from htco_persistence.database import Database, tenant_scope
from htco_persistence.suppliers import PostgresSupplierQuoteStore
from tests.integration.test_etsy_connection import NOW, _url, scalar
from tests.integration.test_order_history import connected

pytestmark = [pytest.mark.db, pytest.mark.anyio]

FIXTURES = Path(__file__).resolve().parents[1] / "application" / "quote_mails"


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


class NoReviews:
    async def check_this_quote(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, message_id: str, reason: str
    ) -> None:
        del organization_id, shop_id, message_id
        raise AssertionError(f"no review expected: {reason}")

    async def possible_forgery(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, day: date
    ) -> None:
        del organization_id, shop_id, day
        raise AssertionError("no forgery expected")


def from_text(text: str) -> QuoteMail:
    return QuoteMail.from_text(
        text, authserv_id="mx.quotes.example.com", sender="quotes@autova.example.com"
    )


def mail(name: str) -> QuoteMail:
    return from_text((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.ac("AC-7.14.1")
@pytest.mark.ac("AC-7.15.3")
async def test_worker__quotes_recorded_once_under_one_request_and_alternatives_marked(
    database: Database, worker_db: Database
) -> None:
    tenant, integration = await connected(database)
    org, shop = tenant.organization_id, integration.shop_id
    intake = QuoteIntake(
        QuoteParser("quotes@autova.example.com"),
        PostgresSupplierQuoteStore(worker_db),
        NoReviews(),
        clock=lambda: NOW,
        owner_mailbox="owner@shop.example.com",
    )
    target = ShopForMail(org, shop)

    with tenant_scope(org):
        first = await intake.handle(mail("01_en_usd.eml.txt"), target)
        again = await intake.handle(mail("01_en_usd.eml.txt"), target)
        second = await intake.handle(mail("03_en_eur_free_shipping.eml.txt"), target)

    assert (first, again, second) == (
        IntakeOutcome.NEW_PRODUCT_QUOTE,
        IntakeOutcome.ALREADY_RECORDED,
        IntakeOutcome.NEW_PRODUCT_QUOTE,
    )
    assert (
        scalar(database, "select count(*) from app.quote_requests where shop_id = %s", [shop]) == 1
    )
    assert (
        scalar(database, "select state from app.quote_requests where shop_id = %s", [shop])
        == "quotes_received"
    )
    assert (
        scalar(
            database, "select count(*) from app.supplier_quotes where organization_id = %s", [org]
        )
        == 2
    )
    assert (
        scalar(database, "select count(*) from app.suppliers where organization_id = %s", [org])
        == 2
    )

    # Map the product to the first supplier: its next quote is an alternative (AC-7.15.3).
    with database.connection() as connection:
        row = connection.execute(
            "select id, supplier_id from app.supplier_quotes"
            " where organization_id = %s and source_reference = 'autova-quote:Q-88213'",
            [org],
        ).fetchone()
        assert row is not None
        connection.execute(
            "insert into app.supplier_mappings"
            " (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)"
            " values (%s, %s, %s, 1234567890, %s, %s, %s)",
            [uuid.uuid7(), org, shop, row[1], row[0], NOW],
        )
    later = (FIXTURES / "01_en_usd.eml.txt").read_text(encoding="utf-8")
    later = later.replace("Q-88213", "Q-88999").replace("7.40 USD", "7.05 USD")

    with tenant_scope(org):
        alternative = await intake.handle(from_text(later), target)
        variant = await intake.handle(mail("05_he_usd_variant.eml.txt"), target)

    assert alternative is IntakeOutcome.ALTERNATIVE
    assert variant is IntakeOutcome.NEW_PRODUCT_QUOTE  # the variant is its own product
