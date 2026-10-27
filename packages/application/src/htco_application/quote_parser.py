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

Only mail from the configured AutoVA sender is read at all (`HTCO_AUTOVA_QUOTE_SENDER`, A-09),
and only when our receiving server (`HTCO_AUTOVA_QUOTE_AUTHSERV_ID`) recorded an aligned DKIM
pass for that sender's domain (`dkim_aligned`): a `From` header alone can be forged. Any other mail
is quarantined (AC-7.14.2); one whose `From` claims AutoVA without that proof also opens a Human
task. The layout is the made-up one of the fixtures
(tests/application/quote_mails/) until the owner's real sample mails arrive.

The application layer uses dataclasses rather than Pydantic (ER-ARCH-01; the deviation recorded in
execution-engine.md §2): every field is validated here and again by `htco_domain.suppliers`.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from email import message_from_string
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr
from enum import StrEnum
from typing import NamedTuple

from htco_domain.orders import Money
from htco_domain.suppliers import MAX_METHOD, MAX_NAME, autova_quote_reference

MAX_MAIL_CHARACTERS = 200_000
MAX_DAYS = 365

_ADDRESS = re.compile(r"^[^@\s<>]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+$")
PLAIN_MESSAGE_ID = re.compile(r"<[A-Za-z0-9._%+=$#-]{1,128}@[A-Za-z0-9.-]{1,128}>")
_MESSAGE_ID = PLAIN_MESSAGE_ID


_AUTHSERV_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
_COMMENT = re.compile(r"\([^()]*\)")
_RESULT = re.compile(r"^(?P<method>[a-z0-9_-]+)\s*=\s*(?P<result>[a-z]+)(?P<properties>(\s.*)?)$")
_PROPERTY = re.compile(r"(?P<name>[a-z0-9_-]+\.[a-z0-9_-]+)\s*=\s*(?P<value>\S+)")


def _domain(address: str) -> str:
    return address.rpartition("@")[2]


def _resinfos(header: str) -> tuple[str, list[str]] | None:
    """An Authentication-Results value as (its authserv-id, its result clauses), comments
    removed; None when it does not read plainly (quoted strings, nested comments, no ID)."""
    text = " ".join(header.split()).casefold()
    if '"' in text or "\\" in text:
        return None
    for _ in range(3):
        text = _COMMENT.sub(" ", text)
    if "(" in text or ")" in text:
        return None
    parts = [part.strip() for part in text.split(";")]
    head = parts[0].split()
    if not head or _AUTHSERV_ID.match(head[0]) is None:
        return None
    return head[0], [part for part in parts[1:] if part]


def dkim_aligned(authentication_results: Sequence[str], *, authserv_id: str, sender: str) -> bool:
    """The receiving server's verdict on a mail, from its Authentication-Results headers in their
    order in the mail (topmost first; RFC 8601). True only when:

    1. exactly one header carries our receiving server's authserv-id (a sender can write these
       headers too; one with our ID that our server did not remove is a forgery, so two fail),
    2. it is the topmost one, the one our server added on arrival, and
    3. it reports `dkim=pass` with `header.d` equal to the configured sender's domain (strict
       DMARC alignment; DKIM survives the mailbox's forwarding, D-23 (a), where SPF does not).

    Anything missing, unreadable or ambiguous is False: the mail is quarantined.
    """
    ours = authserv_id.strip().casefold()
    domain = _domain(_address(sender))
    if _AUTHSERV_ID.match(ours) is None or not domain:
        return False
    read = [_resinfos(header) for header in authentication_results]
    if not read or read[0] is None or read[0][0] != ours:
        return False
    if sum(1 for one in read if one is None or one[0] == ours) != 1:
        return False
    for clause in read[0][1]:
        found = _RESULT.match(clause)
        if found is None or found["method"] != "dkim" or found["result"] != "pass":
            continue
        properties = {p["name"]: p["value"] for p in _PROPERTY.finditer(found["properties"])}
        # The signing domain: header.d, or else the domain of header.i (some receivers give only
        # the identity, for example "header.i=@autova.example.com").
        signed = properties.get("header.d") or _domain(properties.get("header.i", ""))
        if signed == domain:
            return True
    return False


class QuoteMail(NamedTuple):
    """One mail as the intake (7.14) hands it over: never stored or logged by the parser.

    `sender_authenticated` is the receiving side's verdict (`dkim_aligned`), never the sender's
    word: without it the mail is quarantined, whatever its `From` says."""

    sender: str
    subject: str
    message_id: str
    body: str
    sender_authenticated: bool
    # The `To` addresses the sender's DKIM signature covers (`signed_recipients`): the replay
    # guard (#192) reads a quote only when the owner's mailbox is one of them.
    signed_recipients: tuple[str, ...] = ()

    @classmethod
    def from_text(cls, text: str, *, authserv_id: str, sender: str) -> QuoteMail:
        """A whole mail (headers, a blank line, the body; MIME or plain), as the quote mailbox
        fetches it (slice 7.14) and as in the fixtures. The body is the plain-text part; the
        verdict comes from its Authentication-Results headers for our server and the configured
        sender."""
        parsed = message_from_string(text, policy=default_policy)
        results = [str(value) for value in parsed.get_all("Authentication-Results", [])]
        return cls(
            sender=str(parsed.get("From", "")),
            subject=str(parsed.get("Subject", "")),
            message_id=str(parsed.get("Message-ID", "")),
            body=_plain_text(parsed),
            sender_authenticated=dkim_aligned(results, authserv_id=authserv_id, sender=sender),
            signed_recipients=_signed_recipients(parsed, sender=sender),
        )


def _plain_text(message: EmailMessage) -> str:
    """The mail's plain-text body; empty when it has none (the parser then refuses it)."""
    part = message.get_body(preferencelist=("plain",))
    if not isinstance(part, EmailMessage):
        return ""
    if part.get_content_charset() is None and "Content-Transfer-Encoding" not in part:
        # Text as it came (no charset, no encoding: nothing to decode), as in the fixtures.
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    try:
        content = part.get_content()
    except LookupError, ValueError:  # an unknown charset or a broken transfer encoding
        return ""
    return content if isinstance(content, str) else ""


def _dkim_tags(value: str) -> dict[str, str] | None:
    """A DKIM-Signature's tags (RFC 6376 3.2), whitespace removed; None when unreadable."""
    tags: dict[str, str] = {}
    for part in value.split(";"):
        if not part.strip():
            continue
        name, equals, tag_value = part.partition("=")
        name = name.strip().casefold()
        if not equals or not name or name in tags:
            return None
        tags[name] = "".join(tag_value.split())
    return tags


def signed_recipients(text: str, *, sender: str) -> tuple[str, ...]:
    """The `To` addresses covered by the configured sender's DKIM signature (#192, the replay
    guard): a genuine AutoVA mail, validly signed, can be replayed to our mailbox, but its signed
    `To` still names whoever AutoVA sent it to.

    Our receiving server reports the signature's verdict (`dkim_aligned`); this reads only what
    the signature covers. Empty unless all of these hold:

    1. every DKIM-Signature reads, and exactly one has `d=` the sender's domain;
    2. its `h=` lists `from` and `to`;
    3. the mail has exactly one `To` header (a verifier checks the bottom-most one only, so a
       second `To` added on top would be unsigned and could name anyone).
    """
    return _signed_recipients(message_from_string(text, policy=default_policy), sender=sender)


def _signed_recipients(parsed: EmailMessage, *, sender: str) -> tuple[str, ...]:
    domain = _domain(_address(sender))
    signatures = [_dkim_tags(str(value)) for value in parsed.get_all("DKIM-Signature", [])]
    if not domain or any(tags is None for tags in signatures):
        return ()
    ours = [t for t in signatures if t is not None and t.get("d", "").casefold() == domain]
    if len(ours) != 1:
        return ()
    covered = {name.casefold() for name in ours[0].get("h", "").split(":")}
    if not {"from", "to"} <= covered:
        return ()
    to = parsed.get_all("To", [])
    if len(to) != 1:
        return ()
    addresses = [address.strip().lower() for _, address in getaddresses([str(to[0])])]
    return tuple(address for address in addresses if _ADDRESS.match(address))


class RefusalReason(StrEnum):
    NOT_A_QUOTE = "not_a_quote"
    UNKNOWN_FIELD = "unknown_field"
    DUPLICATE_FIELD = "duplicate_field"
    MISSING_FIELD = "missing_field"
    INVALID_VALUE = "invalid_value"
    MIXED_CURRENCIES = "mixed_currencies"
    TOO_LARGE = "too_large"


class QuarantineReason(StrEnum):
    UNKNOWN_SENDER = "unknown_sender"  # another From: kept aside, no task
    # From says AutoVA, but our server saw no aligned DKIM pass: a possible forgery, so one
    # Human task "check this quote" as well.
    UNAUTHENTICATED_SENDER = "unauthenticated_sender"


@dataclass(frozen=True)
class Quarantined:
    """Not from the configured AutoVA sender, or not proven to be: kept aside, never parsed."""

    reason: QuarantineReason
    # Only for an unauthenticated mail (its Human task names it), and only when it reads as a
    # plain message ID: the sender chose it, so no free text reaches the task.
    message_id: str = ""

    @property
    def needs_review(self) -> bool:
        return self.reason is QuarantineReason.UNAUTHENTICATED_SENDER


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
        return autova_quote_reference(self.autova_quote_id)


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
_AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,4})?"  # ASCII digits only
# The money columns are numeric(14, 4): at most 10 digits before the point, 4 after (as read).
_MAX_AMOUNT = Decimal("9999999999.9999")
_MONEY = (
    re.compile(rf"^(?P<amount>{_AMOUNT})\s*(?P<code>[A-Z]{{3}})$"),
    re.compile(rf"^(?P<code>[A-Z]{{3}})\s*(?P<amount>{_AMOUNT})$"),
    re.compile(rf"^(?P<symbol>[$€£₪])\s*(?P<amount>{_AMOUNT})$"),
    re.compile(rf"^(?P<amount>{_AMOUNT})\s*(?P<symbol>[$€£₪])$"),
)
_FREE = frozenset({"free", "חינם"})
_DAYS = re.compile(r"^(?P<low>[0-9]{1,3})(?:\s*[-–]\s*(?P<high>[0-9]{1,3}))?\s*(?:days?|ימים|יום)$")  # noqa: RUF001 - an en dash in a range
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_LISTING = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_LISTING = 2**63 - 1  # the listing_id columns are bigint
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")
_VARIANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_QUOTE_ID = re.compile(r"^[A-Z]{1,4}-[0-9]{1,12}$")  # ASCII digits, as the database's CHECK
# Free text with no control characters or angle brackets, no longer than its column allows.
_NAME = re.compile(rf"^[^\x00-\x1f<>]{{1,{MAX_NAME}}}$")
_METHOD = re.compile(rf"^[^\x00-\x1f<>]{{1,{MAX_METHOD}}}$")


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
        if amount > _MAX_AMOUNT:
            raise _UnreadableError(field)
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


def _listing(field: _Field, value: str) -> int:
    listing = int(_matching(field, value, _LISTING))
    if listing > _MAX_LISTING:
        raise _UnreadableError(field)
    return listing


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
        # Both quarantines come before anything in the body is read.
        if _address(mail.sender) != self._sender:
            return Quarantined(QuarantineReason.UNKNOWN_SENDER)
        if not mail.sender_authenticated:
            plain = _MESSAGE_ID.fullmatch(mail.message_id) is not None
            return Quarantined(
                QuarantineReason.UNAUTHENTICATED_SENDER, mail.message_id if plain else ""
            )
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
            listing_id=_listing(f.LISTING, fields[f.LISTING]),
            variant=_matching(f.VARIANT, fields[f.VARIANT], _VARIANT)
            if f.VARIANT in fields
            else "",
            sku=None if sku is None else _matching(f.SKU, sku, _CODE),
            supplier=_matching(f.SUPPLIER, fields[f.SUPPLIER], _NAME),
            unit_price=unit_price,
            shipping_cost=shipping,
            shipping_method=_matching(f.SHIPPING_METHOD, fields[f.SHIPPING_METHOD], _METHOD),
            processing_days=_days(f.PROCESSING, fields[f.PROCESSING]),
            shipping_days=_days(f.SHIPPING_TIME, fields[f.SHIPPING_TIME]),
            valid_until=None if valid is None else _date(f.VALID_UNTIL, valid),
            autova_quote_id=_matching(f.QUOTE_ID, fields[f.QUOTE_ID], _QUOTE_ID),
        )
