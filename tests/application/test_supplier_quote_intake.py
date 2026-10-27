"""The quote intake (slice 7.15): a parsed quote is recorded under its product's quote request
(one is created for the product's first quote); the same mail twice records one quote; a quote
for a product with an active mapping is an alternative and raises no "new product" alert
(AC-7.15.3); a mail that does not parse opens one Human task, and a quarantined one opens none."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from htco_application.quote_parser import QuoteMail, QuoteParser
from htco_application.suppliers import (
    IntakeOutcome,
    QuoteIntake,
    RecordedQuote,
    ShopForMail,
)
from htco_domain.suppliers import ProductRef, SupplierQuote

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parent / "quote_mails"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("71500000-0000-0000-0000-000000000001")
SHOP = uuid.UUID("71500000-0000-0000-0000-000000000002")


def mail(name: str) -> QuoteMail:
    return QuoteMail.from_text(
        (FIXTURES / name).read_text(encoding="utf-8"),
        authserv_id="mx.quotes.example.com",
        sender="quotes@autova.example.com",
    )


@dataclass
class Store:
    """The quote store in memory, with the database's rules (one quote per request and mail)."""

    mapped: set[str] = field(default_factory=set)
    requests: dict[str, uuid.UUID] = field(default_factory=dict)
    quotes: dict[tuple[uuid.UUID, str], SupplierQuote] = field(default_factory=dict)
    suppliers: dict[str, uuid.UUID] = field(default_factory=dict)

    def record(
        self, organization_id: uuid.UUID, product: ProductRef, supplier_name: str,
        quote: SupplierQuote, *, now: datetime,  # noqa: ARG002 - the port
    ) -> RecordedQuote:  # fmt: skip
        assert organization_id == ORG
        supplier = self.suppliers.setdefault(supplier_name, uuid.uuid7())
        request = self.requests.setdefault(product.key, uuid.uuid7())
        key = (request, quote.source_reference)
        created = key not in self.quotes
        if created:
            self.quotes[key] = replace(quote, quote_request_id=request, supplier_id=supplier)
        return RecordedQuote(
            quote_request_id=request, supplier_id=supplier, created=created,
            alternative=product.key in self.mapped,
        )  # fmt: skip


@dataclass
class Reviews:
    opened: list[tuple[uuid.UUID, uuid.UUID, str, str]] = field(default_factory=list)

    async def check_this_quote(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, *, message_id: str, reason: str
    ) -> None:
        self.opened.append((organization_id, shop_id, message_id, reason))


@dataclass
class World:
    store: Store = field(default_factory=Store)
    reviews: Reviews = field(default_factory=Reviews)

    @property
    def intake(self) -> QuoteIntake:
        return QuoteIntake(
            parser=QuoteParser("quotes@autova.example.com"),
            store=self.store,
            reviews=self.reviews,
            clock=lambda: NOW,
        )


TARGET = ShopForMail(ORG, SHOP)


async def test_a_new_products_first_quote__recorded_and_alerts() -> None:
    world = World()
    outcome = await world.intake.handle(mail("01_en_usd.eml.txt"), TARGET)

    assert outcome is IntakeOutcome.NEW_PRODUCT_QUOTE
    [((request, reference), quote)] = world.store.quotes.items()
    assert reference == "autova-quote:Q-88213"
    assert quote.quote_request_id == request
    assert quote.unit_price.amount == Decimal("7.40")
    assert quote.supplier_id == world.store.suppliers["Guangzhou Leather Works"]
    assert quote.processing_days == (2, 4)
    assert world.reviews.opened == []


async def test_a_second_quote_for_the_same_product__the_same_request() -> None:
    world = World()
    await world.intake.handle(mail("01_en_usd.eml.txt"), TARGET)
    await world.intake.handle(mail("03_en_eur_free_shipping.eml.txt"), TARGET)

    assert len(world.store.requests) == 1
    assert len(world.store.quotes) == 2
    assert set(world.store.suppliers) == {"Guangzhou Leather Works", "Porto Craft Supplies"}


@pytest.mark.ac("AC-7.14.1")
async def test_the_same_mail_twice__one_quote() -> None:
    world = World()
    first = await world.intake.handle(mail("02_he_ils.eml.txt"), TARGET)
    again = await world.intake.handle(mail("02_he_ils.eml.txt"), TARGET)

    assert first is IntakeOutcome.NEW_PRODUCT_QUOTE
    assert again is IntakeOutcome.ALREADY_RECORDED
    assert len(world.store.quotes) == 1


@pytest.mark.ac("AC-7.15.3")
async def test_a_mapped_product__an_alternative_without_a_new_product_alert() -> None:
    world = World()
    world.store.mapped.add(ProductRef(SHOP, 1234567890).key)

    outcome = await world.intake.handle(mail("01_en_usd.eml.txt"), TARGET)

    assert outcome is IntakeOutcome.ALTERNATIVE
    assert len(world.store.quotes) == 1


async def test_a_variant__is_its_own_product() -> None:
    world = World()
    await world.intake.handle(mail("01_en_usd.eml.txt"), TARGET)
    await world.intake.handle(mail("05_he_usd_variant.eml.txt"), TARGET)

    assert len(world.store.requests) == 2


@pytest.mark.ac("AC-7.15.1")
@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("07_changed_layout.eml.txt", "unknown_field"),
        ("08_mixed_currencies.eml.txt", "mixed_currencies"),
        ("10_duplicate_field.eml.txt", "duplicate_field"),
        ("11_missing_price.eml.txt", "missing_field"),
        ("12_bad_values.eml.txt", "invalid_value"),
    ],
)
async def test_a_mail_that_does_not_parse__one_human_task_and_no_quote(
    name: str, reason: str
) -> None:
    world = World()
    outcome = await world.intake.handle(mail(name), TARGET)

    assert outcome is IntakeOutcome.NEEDS_REVIEW
    assert world.store.quotes == {}
    [(organization, shop, message_id, why)] = world.reviews.opened
    assert (organization, shop, why) == (ORG, SHOP, reason)
    assert message_id.startswith("<q-")


@pytest.mark.ac("AC-7.14.2")
async def test_another_sender__quarantined_no_quote_and_no_task() -> None:
    world = World()
    outcome = await world.intake.handle(mail("09_other_sender.eml.txt"), TARGET)

    assert outcome is IntakeOutcome.QUARANTINED
    assert world.store.quotes == {}
    assert world.reviews.opened == []


@pytest.mark.ac("AC-7.14.2")
@pytest.mark.parametrize(
    "name",
    [
        "13_forged_from_unsigned.eml.txt",
        "14_no_authentication_results.eml.txt",
        "15_forged_results_below_ours.eml.txt",
        "16_results_from_another_server.eml.txt",
        "17_dkim_pass_for_another_domain.eml.txt",
        "18_dkim_fail.eml.txt",
    ],
)
async def test_a_forged_autova_sender__quarantined_no_quote_and_one_human_task(name: str) -> None:
    """From says AutoVA but our server saw no aligned DKIM pass: nothing is recorded, and one
    Human task names the mail and the reason, never anything from its body."""
    world = World()
    outcome = await world.intake.handle(mail(name), TARGET)

    assert outcome is IntakeOutcome.QUARANTINED
    assert world.store.quotes == {}
    assert world.store.suppliers == {}
    [(organization, shop, message_id, why)] = world.reviews.opened
    assert (organization, shop, why) == (ORG, SHOP, "unauthenticated_sender")
    assert message_id.startswith("<forged-")
