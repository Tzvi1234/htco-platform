"""Fulfillment (Phase 6): tracking codes, business-day calendars and SLA windows. No I/O.

- **Tracking (slice 6.1, AC-6.10.4):** a tracking code is normalised (spaces and dashes out,
  upper case) and checked against its carrier's format before HTCO records or uploads it. The code
  can locate the parcel and so the buyer: it is never in a repr or a log, and the tables outside
  the buyer-data ones keep only its fingerprint and last four characters.
- **SLA windows (slice 6.2, FR-INC-01):** a policy gives the realistic window from payment to the
  first tracking, in calendar or business days. A business-day window skips the calendar's
  weekend days and holidays. An order breaches only *after* its deadline: at exactly the deadline
  it is still inside its window (AC-6.2.1).
"""

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

# --- Tracking codes ------------------------------------------------------------------------------


class TrackingFormatError(ValueError):
    """The carrier or tracking code is not valid; the message is safe to show (no code in it)."""


class Carrier(StrEnum):
    """Carriers HTCO validates. `OTHER` accepts any plausible code (Etsy's "Other" carrier)."""

    USPS = "usps"
    UPS = "ups"
    FEDEX = "fedex"
    DHL = "dhl"
    YUN_EXPRESS = "yun_express"
    CHINA_POST = "china_post"
    FOUR_PX = "4px"
    YANWEN = "yanwen"
    CAINIAO = "cainiao"
    ISRAEL_POST = "israel_post"
    OTHER = "other"


# Formats from the carriers' public documentation; deliberately tolerant where a carrier has many.
_UPU = r"[A-Z]{2}\d{9}[A-Z]{2}"  # The Universal Postal Union's S10 format, used by every post.
_FORMATS: dict[Carrier, re.Pattern[str]] = {
    Carrier.USPS: re.compile(rf"^(\d{{20,22}}|9\d{{15,25}}|{_UPU})$"),
    Carrier.UPS: re.compile(r"^(1Z[0-9A-Z]{16}|T\d{10}|\d{9}|\d{26})$"),
    Carrier.FEDEX: re.compile(r"^(\d{12}|\d{15}|\d{20}|\d{22})$"),
    Carrier.DHL: re.compile(
        rf"^(\d{{10,11}}|JJD\d{{18,20}}|JVGL\d{{16,20}}|GM\d{{16,22}}|{_UPU})$"
    ),
    Carrier.YUN_EXPRESS: re.compile(rf"^(YT\d{{16}}|{_UPU})$"),
    Carrier.CHINA_POST: re.compile(rf"^{_UPU}$"),
    Carrier.FOUR_PX: re.compile(rf"^(4PX\d{{12,16}}[A-Z]{{0,2}}|{_UPU})$"),
    Carrier.YANWEN: re.compile(rf"^(Y[A-Z]\d{{9}}[A-Z]{{2}}|UG\d{{9}}YP|{_UPU})$"),
    Carrier.CAINIAO: re.compile(rf"^(LP\d{{14,16}}|CAINIAO[0-9A-Z]{{6,20}}|{_UPU})$"),
    Carrier.ISRAEL_POST: re.compile(rf"^{_UPU}$"),
    Carrier.OTHER: re.compile(r"^[A-Z0-9]{8,40}$"),
}
# How AutoVA and Etsy name carriers, mapped to ours (lower case, spaces and dashes removed).
_ALIASES = {
    "usps": Carrier.USPS,
    "ups": Carrier.UPS,
    "fedex": Carrier.FEDEX,
    "dhl": Carrier.DHL,
    "dhlexpress": Carrier.DHL,
    "dhlecommerce": Carrier.DHL,
    "yunexpress": Carrier.YUN_EXPRESS,
    "yun": Carrier.YUN_EXPRESS,
    "chinapost": Carrier.CHINA_POST,
    "chinaems": Carrier.CHINA_POST,
    "4px": Carrier.FOUR_PX,
    "yanwen": Carrier.YANWEN,
    "cainiao": Carrier.CAINIAO,
    "israelpost": Carrier.ISRAEL_POST,
    "other": Carrier.OTHER,
}
_SEPARATORS = re.compile(r"[\s\-]+")
TRACKING_CODE_MAX = 40


def carrier_of(name: str) -> Carrier:
    """Our carrier for a name as Etsy or AutoVA writes it ("Yun Express", "USPS"); unknown
    names are `OTHER`, so a carrier Etsy knows and HTCO does not still records."""
    key = _SEPARATORS.sub("", name.strip().lower()).replace("_", "")
    return _ALIASES.get(key, Carrier.OTHER)


def normalize_tracking_code(code: str) -> str:
    return _SEPARATORS.sub("", code).upper()


@dataclass(frozen=True)
class TrackingNumber:
    """A validated tracking number. The code never appears in a repr."""

    carrier: Carrier
    code: str = field(repr=False)

    @classmethod
    def parse(cls, carrier: str, code: str) -> TrackingNumber:
        """Validates a carrier and code typed by a person or read from a supplier (AC-6.10.4)."""
        try:
            parsed = Carrier(carrier.strip().lower())
        except ValueError:
            raise TrackingFormatError("unknown carrier: pick one from the list") from None
        normalized = normalize_tracking_code(code)
        if not normalized or len(normalized) > TRACKING_CODE_MAX:
            raise TrackingFormatError("the tracking number is empty or too long")
        if not _FORMATS[parsed].match(normalized):
            raise TrackingFormatError(f"this is not a valid {parsed.value} tracking number")
        return cls(parsed, normalized)

    @property
    def last4(self) -> str:
        return self.code[-4:]

    def fingerprint(self, shop_id: uuid.UUID) -> bytes:
        return tracking_fingerprint(shop_id, self.code)


def tracking_fingerprint(shop_id: uuid.UUID, code: str) -> bytes:
    """SHA-256 of the shop and the normalised code: one per parcel, whatever the carrier is
    called, so the same number is never recorded or uploaded twice (AC-6.1.1, AC-6.10.2)."""
    material = f"htco-tracking:v1:{shop_id}:{normalize_tracking_code(code)}"
    return hashlib.sha256(material.encode()).digest()


# --- Business-day calendars and SLA windows ------------------------------------------------------

SATURDAY, SUNDAY = 5, 6  # date.weekday()


@dataclass(frozen=True)
class BusinessCalendar:
    """Which days count. The time zone decides where a day starts (the shop's own)."""

    timezone: str = "UTC"
    weekend: frozenset[int] = frozenset({SATURDAY, SUNDAY})
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if not self.weekend <= frozenset(range(7)) or len(self.weekend) > 6:  # noqa: PLR2004 - a week
            raise ValueError("a weekend is a set of weekday numbers 0..6 with one working day")
        ZoneInfo(self.timezone)  # raises for an unknown zone

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def add_business_days(self, start: datetime, days: int) -> datetime:
        """The moment `days` business days after `start`. A start on a non-business day counts
        from the next business day's beginning, so a Saturday order waits for Monday."""
        if days < 0:
            raise ValueError("a window is not negative")
        zone = ZoneInfo(self.timezone)
        local = start.astimezone(zone)
        day = local.date()
        if not self.is_business_day(day):
            while not self.is_business_day(day):
                day += timedelta(days=1)
            local = datetime.combine(day, time.min, tzinfo=zone)
        counted = 0
        while counted < days:
            day += timedelta(days=1)
            if self.is_business_day(day):
                counted += 1
        return datetime.combine(day, local.timetz()).astimezone(start.tzinfo)


class WindowUnit(StrEnum):
    CALENDAR_DAYS = "calendar_days"
    BUSINESS_DAYS = "business_days"


@dataclass(frozen=True)
class SlaPolicy:
    """The window from payment to the first tracking for a shop, optionally for one supplier
    and one shipping method (the most specific policy wins)."""

    id: uuid.UUID
    window: int
    unit: WindowUnit
    calendar: BusinessCalendar = BusinessCalendar()
    shop_id: uuid.UUID | None = None  # None: the organization's default
    supplier: str | None = None
    shipping_method: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("an SLA window is at least one day")

    def deadline(self, paid_at: datetime) -> datetime:
        if self.unit is WindowUnit.CALENDAR_DAYS:
            return paid_at + timedelta(days=self.window)
        return self.calendar.add_business_days(paid_at, self.window)

    def is_breached(self, paid_at: datetime, *, now: datetime) -> bool:
        """Strictly after the deadline (AC-6.2.1)."""
        return now > self.deadline(paid_at)

    @property
    def specificity(self) -> int:
        return (
            (self.shop_id is not None)
            + 2 * (self.supplier is not None)
            + 4 * (self.shipping_method is not None)
        )


# The owner's default (decision of 2026-10-23, replacing the first generous ten): seven business
# days from payment to the first tracking, until a policy row names another window.
DEFAULT_WINDOW_DAYS = 7


def default_policy() -> SlaPolicy:
    return SlaPolicy(
        id=uuid.UUID(int=0), window=DEFAULT_WINDOW_DAYS, unit=WindowUnit.BUSINESS_DAYS, version=0
    )


def select_policy(
    policies: Iterable[SlaPolicy],
    *,
    shop_id: uuid.UUID,
    supplier: str | None = None,
    shipping_method: str | None = None,
) -> SlaPolicy:
    """The most specific policy that matches the order; the built-in default otherwise."""
    matching = [
        p
        for p in policies
        if p.shop_id in {None, shop_id}
        and p.supplier in {None, supplier}
        and p.shipping_method in {None, shipping_method}
    ]
    if not matching:
        return default_policy()
    return max(matching, key=lambda p: (p.specificity, p.version))


# --- Ship-by warnings (slice 6.9) ----------------------------------------------------------------


def ship_by_at_risk(
    expected_ship: datetime,
    *,
    now: datetime,
    warning_business_days: int,
    calendar: BusinessCalendar,
) -> bool:
    """True from `warning_business_days` business days before the ship-by date (inclusive)
    until it, and after it: an order without a shipment is at risk once inside that window."""
    warn_from = expected_ship
    zone = ZoneInfo(calendar.timezone)
    day = expected_ship.astimezone(zone).date()
    counted = 0
    while counted < warning_business_days:
        day -= timedelta(days=1)
        if calendar.is_business_day(day):
            counted += 1
    if warning_business_days:
        warn_from = datetime.combine(day, time.min, tzinfo=zone)
    return now >= warn_from
