"""The deterministic AutoVA quote parser (slice 7.15): code, never a model.

AutoVA mails each supplier quote. This module reads one known layout: a block of `Label: value`
lines, in English or in Hebrew, with exactly the fields below. Anything else is refused, with its
reason, and the intake opens one Human task "check this quote". A quote is never guessed
(AC-7.15.1):

- a line `Label: value` whose label is not one of the known ones (the layout changed);
- a required field missing, or any field twice;
- a value that does not read exactly (a price with a comma decimal, a range the wrong way round,
  a date that is not ISO, an identifier with odd characters);
- a price and a shipping cost in two currencies.

Only mail from the configured AutoVA sender is read at all (`HTCO_AUTOVA_QUOTE_SENDER`, A-09);
any other is quarantined (AC-7.14.2). The layout is the made-up one of the fixtures
(tests/application/quote_mails/) until the owner's real sample mails arrive.

The application layer uses dataclasses rather than Pydantic (ER-ARCH-01; the deviation recorded in
execution-engine.md §2): every field is validated here and again by `htco_domain.suppliers`.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from email import message_from_string
from email.policy import default as default_policy
from email.utils import parseaddr
from enum import StrEnum
from typing import NamedTuple

from htco_domain.orders import Money

MAX_MAIL_CHARACTERS = 200_000
MAX_DAYS = 365

_ADDRESS = re.compile(r"^[^@\s<>]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+$")


class QuoteMail(NamedTuple):
    """One mail as the intake (7.14) hands it over: never stored or logged by the parser."""

    sender: str
    subject: str
    message_id: str
    body: str

    @classmethod
    def from_text(cls, text: str) -> QuoteMail:
        """A plain-text mail (headers, a blank line, the body), as in the fixtures."""
        parsed = message_from_string(text, policy=default_policy)
        body = parsed.get_payload()
        return cls(
            sender=str(parsed.get("From", "")),
            subject=str(parsed.get("Subject", "")),
            message_id=str(parsed.get("Message-ID", "")),
            body=body if isinstance(body, str) else "",
        )


class RefusalReason(StrEnum):
    NOT_A_QUOTE = "not_a_quote"
    UNKNOWN_FIELD = "unknown_field"
    DUPLICATE_FIELD = "duplicate_field"
    MISSING_FIELD = "missing_field"
    INVALID_VALUE = "invalid_value"
    MIXED_CURRENCIES = "mixed_currencies"
    TOO_LARGE = "too_large"


@dataclass(frozen=True)
class Quarantined:
    """Not from the configured AutoVA sender: kept aside and counted, never parsed."""

    reason: str


@dataclass(frozen=True)
class Refused:
    """From AutoVA but not in the known layout: one Human task, never a quote."""

    reason: RefusalReason
    message_id: str
    field: str | None = None  # which field, when one is to blame (a code, never its value)


@dataclass(frozen=True)
class ParsedQuote:
    autova_product: str
    listing_id: int
    variant: str
    sku: str | None
    supplier: str
    unit_price: Money
    shipping_cost: Money
    shipping_method: str
    processing_days: tuple[int, int]
    shipping_days: tuple[int, int]
    valid_until: date | None
    autova_quote_id: str

    @property
    def landed_cost(self) -> Money:
        return Money(self.unit_price.amount + self.shipping_cost.amount, self.unit_price.currency)

    @property
    def source_reference(self) -> str:
        """Stable per AutoVA quote, so the same mail twice stores one quote."""
        return f"autova-quote:{self.autova_quote_id}"


type ParseResult = ParsedQuote | Refused | Quarantined


class _Field(StrEnum):
    PRODUCT = "product"
    LISTING = "listing"
    SKU = "sku"
    VARIANT = "variant"
    SUPPLIER = "supplier"
    UNIT_PRICE = "unit_price"
    SHIPPING = "shipping"
    SHIPPING_METHOD = "shipping_method"
    PROCESSING = "processing"
    SHIPPING_TIME = "shipping_time"
    VALID_UNTIL = "valid_until"
    QUOTE_ID = "quote_id"


_LABELS: dict[str, _Field] = {
    # English
    "product": _Field.PRODUCT,
    "etsy listing": _Field.LISTING,
    "sku": _Field.SKU,
    "variant": _Field.VARIANT,
    "supplier": _Field.SUPPLIER,
    "unit price": _Field.UNIT_PRICE,
    "shipping": _Field.SHIPPING,
    "shipping method": _Field.SHIPPING_METHOD,
    "processing time": _Field.PROCESSING,
    "shipping time": _Field.SHIPPING_TIME,
    "valid until": _Field.VALID_UNTIL,
    "quote id": _Field.QUOTE_ID,
    # Hebrew
    "מוצר": _Field.PRODUCT,
    "רישום etsy": _Field.LISTING,
    "מק״ט": _Field.SKU,  # noqa: RUF001 - a Hebrew label
    'מק"ט': _Field.SKU,  # noqa: RUF001 - a Hebrew label
    "וריאציה": _Field.VARIANT,
    "ספק": _Field.SUPPLIER,
    "מחיר ליחידה": _Field.UNIT_PRICE,
    "משלוח": _Field.SHIPPING,
    "שיטת משלוח": _Field.SHIPPING_METHOD,
    "זמן טיפול": _Field.PROCESSING,
    "זמן משלוח": _Field.SHIPPING_TIME,
    "בתוקף עד": _Field.VALID_UNTIL,
    "מזהה הצעה": _Field.QUOTE_ID,
}
_OPTIONAL = frozenset({_Field.SKU, _Field.VARIANT, _Field.VALID_UNTIL})

# A field line: a short label (at most four words, no sentence punctuation), a colon, a value.
_LINE = re.compile(r"^\s*(?P<label>[^:.!?,]{1,40}?)\s*:\s*(?P<value>\S.*?)\s*$")
_WORDS_IN_A_LABEL = 4

_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₪": "ILS"}
_CURRENCIES = frozenset({"USD", "EUR", "GBP", "ILS", "CNY", "CAD", "AUD", "JPY", "HKD", "TRY"})
_AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,4})?"
_MONEY = (
    re.compile(rf"^(?P<amount>{_AMOUNT})\s*(?P<code>[A-Z]{{3}})$"),
    re.compile(rf"^(?P<code>[A-Z]{{3}})\s*(?P<amount>{_AMOUNT})$"),
    re.compile(rf"^(?P<symbol>[$€£₪])\s*(?P<amount>{_AMOUNT})$"),
    re.compile(rf"^(?P<amount>{_AMOUNT})\s*(?P<symbol>[$€£₪])$"),
)
_FREE = frozenset({"free", "חינם"})
_DAYS = re.compile(r"^(?P<low>\d{1,3})(?:\s*[-–]\s*(?P<high>\d{1,3}))?\s*(?:days?|ימים|יום)$")  # noqa: RUF001 - an en dash in a range
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LISTING = re.compile(r"^[1-9]\d{0,18}$")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")
_VARIANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_QUOTE_ID = re.compile(r"^[A-Z]{1,4}-\d{1,12}$")
_TEXT = re.compile(r"^[^\x00-\x1f<>]{1,200}$")


class _UnreadableError(ValueError):
    """A value that does not read exactly; `field` names it."""

    def __init__(self, field: _Field) -> None:
        super().__init__(field.value)
        self.field = field


def _money(field: _Field, value: str) -> Money:
    for pattern in _MONEY:
        found = pattern.match(value)
        if found is None:
            continue
        groups = found.groupdict()
        code = groups.get("code") or _SYMBOLS[groups["symbol"]]
        if code not in _CURRENCIES:
            raise _UnreadableError(field)
        try:
            amount = Decimal(groups["amount"].replace(",", ""))
        except InvalidOperation:
            raise _UnreadableError(field) from None
        return Money(amount, code)
    raise _UnreadableError(field)


def _days(field: _Field, value: str) -> tuple[int, int]:
    found = _DAYS.match(value)
    if found is None:
        raise _UnreadableError(field)
    low = int(found["low"])
    high = int(found["high"]) if found["high"] is not None else low
    if not 0 <= low <= high <= MAX_DAYS:
        raise _UnreadableError(field)
    return low, high


def _matching(field: _Field, value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.match(value):
        raise _UnreadableError(field)
    return value


def _date(field: _Field, value: str) -> date:
    if not _ISO_DATE.match(value):
        raise _UnreadableError(field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _UnreadableError(field) from None


def _address(value: str) -> str:
    return parseaddr(value.strip())[1].strip().lower()


class QuoteParser:
    """Reads AutoVA's quote mails from the configured sender only."""

    def __init__(self, sender: str) -> None:
        address = _address(sender)
        if not _ADDRESS.match(address):
            raise ValueError("the AutoVA quote sender must be an email address")
        self._sender = address

    def parse(self, mail: QuoteMail) -> ParseResult:  # noqa: PLR0911 - one return per refusal
        if _address(mail.sender) != self._sender:
            return Quarantined("unknown_sender")
        if len(mail.body) > MAX_MAIL_CHARACTERS:
            return Refused(RefusalReason.TOO_LARGE, mail.message_id)
        fields: dict[_Field, str] = {}
        for line in mail.body.splitlines():
            found = _LINE.match(line)
            if found is None:
                continue
            label = found["label"].strip().casefold()
            if len(label.split()) > _WORDS_IN_A_LABEL:
                continue  # a sentence with a colon, not a field
            field = _LABELS.get(label)
            if field is None:
                return Refused(RefusalReason.UNKNOWN_FIELD, mail.message_id)
            if field in fields:
                return Refused(RefusalReason.DUPLICATE_FIELD, mail.message_id, field.value)
            fields[field] = found["value"]
        if not fields:
            return Refused(RefusalReason.NOT_A_QUOTE, mail.message_id)
        missing = [f for f in _Field if f not in _OPTIONAL and f not in fields]
        if missing:
            return Refused(RefusalReason.MISSING_FIELD, mail.message_id, missing[0].value)
        try:
            return self._quote(fields, mail.message_id)
        except _UnreadableError as unreadable:
            return Refused(RefusalReason.INVALID_VALUE, mail.message_id, unreadable.field.value)

    @staticmethod
    def _quote(fields: dict[_Field, str], message_id: str) -> ParsedQuote | Refused:
        f = _Field
        unit_price = _money(f.UNIT_PRICE, fields[f.UNIT_PRICE])
        shipping_text = fields[f.SHIPPING]
        shipping = (
            Money(Decimal(0), unit_price.currency)
            if shipping_text.casefold() in _FREE
            else _money(f.SHIPPING, shipping_text)
        )
        if shipping.currency != unit_price.currency:
            return Refused(RefusalReason.MIXED_CURRENCIES, message_id, f.SHIPPING.value)
        if unit_price.amount <= 0:
            raise _UnreadableError(f.UNIT_PRICE)
        valid = fields.get(f.VALID_UNTIL)
        sku = fields.get(f.SKU)
        return ParsedQuote(
            autova_product=_matching(f.PRODUCT, fields[f.PRODUCT], _CODE),
            listing_id=int(_matching(f.LISTING, fields[f.LISTING], _LISTING)),
            variant=_matching(f.VARIANT, fields[f.VARIANT], _VARIANT)
            if f.VARIANT in fields
            else "",
            sku=None if sku is None else _matching(f.SKU, sku, _CODE),
            supplier=_matching(f.SUPPLIER, fields[f.SUPPLIER], _TEXT),
            unit_price=unit_price,
            shipping_cost=shipping,
            shipping_method=_matching(f.SHIPPING_METHOD, fields[f.SHIPPING_METHOD], _TEXT),
            processing_days=_days(f.PROCESSING, fields[f.PROCESSING]),
            shipping_days=_days(f.SHIPPING_TIME, fields[f.SHIPPING_TIME]),
            valid_until=None if valid is None else _date(f.VALID_UNTIL, valid),
            autova_quote_id=_matching(f.QUOTE_ID, fields[f.QUOTE_ID], _QUOTE_ID),
        )
