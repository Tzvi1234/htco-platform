"""The deterministic AutoVA quote parser (slice 7.15), against the made-up golden mails in
tests/application/quote_mails/ (AC-7.15.1): every sample parses to its expected quote; a changed
layout, a missing or repeated field, a value that does not read exactly, or mixed currencies is
refused, never guessed; mail from any sender but the configured one is quarantined."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from htco_application.quote_parser import (
    ParsedQuote,
    Quarantined,
    QuoteMail,
    QuoteParser,
    RefusalReason,
    Refused,
)

FIXTURES = Path(__file__).resolve().parent / "quote_mails"
EXPECTED: dict[str, dict[str, Any]] = json.loads(
    (FIXTURES / "expected.json").read_text(encoding="utf-8")
)
SENDER = "quotes@autova.example.com"


def mail(name: str) -> QuoteMail:
    return QuoteMail.from_text((FIXTURES / name).read_text(encoding="utf-8"))


def as_json(quote: ParsedQuote) -> dict[str, Any]:
    return {
        "autova_product": quote.autova_product,
        "listing_id": quote.listing_id,
        "variant": quote.variant,
        "sku": quote.sku,
        "supplier": quote.supplier,
        "currency": quote.unit_price.currency,
        "unit_price": str(quote.unit_price.amount),
        "shipping_cost": str(quote.shipping_cost.amount),
        "shipping_method": quote.shipping_method,
        "processing_days": list(quote.processing_days),
        "shipping_days": list(quote.shipping_days),
        "valid_until": None if quote.valid_until is None else quote.valid_until.isoformat(),
        "autova_quote_id": quote.autova_quote_id,
    }


def test_every_fixture_has_its_expected_result() -> None:
    assert sorted(EXPECTED) == sorted(p.name for p in FIXTURES.glob("*.eml.txt"))


@pytest.mark.ac("AC-7.15.1")
@pytest.mark.parametrize("name", sorted(n for n, e in EXPECTED.items() if "quote" in e))
def test_golden__each_sample_parses_to_its_expected_quote(name: str) -> None:
    result = QuoteParser(SENDER).parse(mail(name))
    assert isinstance(result, ParsedQuote), result
    assert as_json(result) == EXPECTED[name]["quote"]


@pytest.mark.ac("AC-7.15.1")
@pytest.mark.parametrize("name", sorted(n for n, e in EXPECTED.items() if "refused" in e))
def test_golden__a_mail_that_does_not_parse_exactly_is_refused(name: str) -> None:
    result = QuoteParser(SENDER).parse(mail(name))
    assert isinstance(result, Refused), result
    assert result.reason is RefusalReason(EXPECTED[name]["refused"])
    assert result.message_id  # so the Human task can point at the mail


@pytest.mark.ac("AC-7.14.2")
def test_another_sender__is_quarantined_and_never_parsed() -> None:
    result = QuoteParser(SENDER).parse(mail("09_other_sender.eml.txt"))
    assert result == Quarantined("unknown_sender")


@pytest.mark.parametrize(
    "sender",
    [
        "QUOTES@AutoVA.example.com",
        "AutoVA <quotes@autova.example.com>",
        " quotes@autova.example.com ",
    ],
)
def test_the_sender__compared_as_an_address(sender: str) -> None:
    parsed = mail("01_en_usd.eml.txt")
    assert isinstance(QuoteParser(SENDER).parse(parsed._replace(sender=sender)), ParsedQuote)


@pytest.mark.parametrize("configured", ["", "not an address", "a@b"])
def test_the_configured_sender__must_be_an_address(configured: str) -> None:
    with pytest.raises(ValueError, match="sender"):
        QuoteParser(configured)


def test_money__exact_decimals_never_floats() -> None:
    quote = QuoteParser(SENDER).parse(mail("04_en_thousands_no_validity.eml.txt"))
    assert isinstance(quote, ParsedQuote)
    assert quote.unit_price.amount == Decimal("1240.00")
    assert isinstance(quote.unit_price.amount, Decimal)
    assert quote.landed_cost.amount == Decimal("1326.50")


def test_the_source_reference__is_the_autova_quote_not_the_mail_content() -> None:
    quote = QuoteParser(SENDER).parse(mail("01_en_usd.eml.txt"))
    assert isinstance(quote, ParsedQuote)
    assert quote.source_reference == "autova-quote:Q-88213"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Unit price: 7.40 USD", "Unit price: 0 USD"),
        ("Unit price: 7.40 USD", "Unit price: -7.40 USD"),
        ("Unit price: 7.40 USD", "Unit price: 7.40 usd"),
        ("Unit price: 7.40 USD", "Unit price: 7.40 XXXX"),
        ("Unit price: 7.40 USD", "Unit price: 7,40 USD"),
        ("Etsy listing: 1234567890", "Etsy listing: 12345abc"),
        ("Processing time: 2-4 days", "Processing time: soon"),
        ("Shipping time: 7-12 days", "Shipping time: 7-400 days"),
        ("Quote ID: Q-88213", "Quote ID: <script>"),
    ],
)
def test_a_value_that_does_not_read_exactly__is_refused(field: str, value: str) -> None:
    text = (FIXTURES / "01_en_usd.eml.txt").read_text(encoding="utf-8").replace(field, value)
    result = QuoteParser(SENDER).parse(QuoteMail.from_text(text))
    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.INVALID_VALUE


def test_a_mail_with_no_quote_fields__is_not_a_quote() -> None:
    text = "From: quotes@autova.example.com\nSubject: Hi\nMessage-ID: <m@x>\n\nHello, see you soon."
    result = QuoteParser(SENDER).parse(QuoteMail.from_text(text))
    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.NOT_A_QUOTE


def test_a_huge_mail__is_refused_before_parsing() -> None:
    text = (FIXTURES / "01_en_usd.eml.txt").read_text(encoding="utf-8") + ("x" * 300_000)
    result = QuoteParser(SENDER).parse(QuoteMail.from_text(text))
    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.TOO_LARGE
