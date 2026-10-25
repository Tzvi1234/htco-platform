"""Tracking codes, business-day calendars and SLA windows (slices 6.1, 6.2, 6.9, 6.10)."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_domain.fulfillment import (
    DEFAULT_WINDOW_DAYS,
    BusinessCalendar,
    Carrier,
    SlaPolicy,
    TrackingFormatError,
    TrackingNumber,
    WindowUnit,
    carrier_of,
    default_policy,
    select_policy,
    ship_by_at_risk,
    tracking_fingerprint,
)

SHOP = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_SHOP = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
# 2026-10-23 is a Friday.
FRIDAY = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)


def _policy(
    window: int = 3, unit: WindowUnit = WindowUnit.BUSINESS_DAYS, **kwargs: object
) -> SlaPolicy:
    return SlaPolicy(id=uuid.uuid4(), window=window, unit=unit, **kwargs)  # type: ignore[arg-type]


# --- Tracking numbers (AC-6.10.4) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("carrier", "code"),
    [
        ("usps", "9400 1000 0000 0000 0000 00"),
        ("ups", "1Z999AA10123456784"),
        ("fedex", "123456789012"),
        ("dhl", "1234567890"),
        ("yun_express", "YT2412345678901234"),
        ("china_post", "LX123456789CN"),
        ("israel_post", "RR123456789IL"),
        ("other", "ABCD1234EFGH"),
    ],
)
def test_ac_6_10_4_valid_tracking_numbers_parse_normalised(carrier: str, code: str) -> None:
    number = TrackingNumber.parse(carrier, code)
    assert number.carrier == Carrier(carrier)
    assert " " not in number.code
    assert number.code == number.code.upper()


@pytest.mark.parametrize(
    ("carrier", "code", "message"),
    [
        ("pigeon", "LX123456789CN", "unknown carrier"),
        ("china_post", "", "empty or too long"),
        ("china_post", "A" * 41, "empty or too long"),
        ("ups", "123", "not a valid ups"),
        ("yun_express", "hello world", "not a valid yun_express"),
    ],
)
def test_ac_6_10_4_invalid_tracking_is_rejected_with_a_clear_message(
    carrier: str, code: str, message: str
) -> None:
    with pytest.raises(TrackingFormatError, match=message) as raised:
        TrackingNumber.parse(carrier, code)
    assert code not in str(raised.value) or not code


def test_tracking_code_is_never_in_a_repr() -> None:
    number = TrackingNumber.parse("china_post", "LX123456789CN")
    assert "LX123456789CN" not in repr(number)
    assert number.last4 == "89CN"


def test_carrier_names_from_etsy_and_autova_map_to_ours() -> None:
    assert carrier_of("Yun Express") is Carrier.YUN_EXPRESS
    assert carrier_of("USPS") is Carrier.USPS
    assert carrier_of("china-post") is Carrier.CHINA_POST
    assert carrier_of("Some Local Courier") is Carrier.OTHER


def test_ac_6_1_1_fingerprint_is_one_per_parcel_and_shop() -> None:
    a = tracking_fingerprint(SHOP, "lx 1234-56789cn")
    assert a == tracking_fingerprint(SHOP, "LX123456789CN")
    assert a != tracking_fingerprint(OTHER_SHOP, "LX123456789CN")
    assert len(a) == 32


# --- Business days (AC-6.2.2) ---------------------------------------------------------------------


def test_ac_6_2_2_business_days_skip_the_weekend() -> None:
    calendar = BusinessCalendar()
    # Friday + 1 business day = Monday, same time of day.
    assert calendar.add_business_days(FRIDAY, 1) == datetime(2026, 10, 26, 15, 30, tzinfo=UTC)


def test_ac_6_2_2_business_days_skip_configured_holidays() -> None:
    calendar = BusinessCalendar(holidays=frozenset({date(2026, 10, 26)}))
    assert calendar.add_business_days(FRIDAY, 1) == datetime(2026, 10, 27, 15, 30, tzinfo=UTC)


def test_ac_6_2_2_an_order_paid_on_a_weekend_counts_from_the_next_business_day() -> None:
    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    calendar = BusinessCalendar()
    assert calendar.add_business_days(saturday, 1) == datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    assert calendar.add_business_days(saturday, 0) == datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


def test_a_custom_weekend_and_time_zone() -> None:
    # An Israeli week: Friday and Saturday off; the day starts in Jerusalem.
    calendar = BusinessCalendar(timezone="Asia/Jerusalem", weekend=frozenset({4, 5}))
    thursday = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert calendar.add_business_days(thursday, 1).date() == date(2026, 10, 25)


def test_calendars_refuse_nonsense() -> None:
    with pytest.raises(ValueError, match="weekend"):
        BusinessCalendar(weekend=frozenset(range(7)))
    with pytest.raises(ValueError, match="weekend"):
        BusinessCalendar(weekend=frozenset({9}))
    with pytest.raises(ValueError, match="negative"):
        BusinessCalendar().add_business_days(FRIDAY, -1)
    with pytest.raises(ValueError, match="at least one day"):
        _policy(window=0)


@given(st.integers(min_value=0, max_value=60), st.integers(min_value=0, max_value=6))
def test_business_deadlines_never_land_on_a_weekend(days: int, offset: int) -> None:
    start = FRIDAY + timedelta(days=offset)
    deadline = BusinessCalendar().add_business_days(start, days)
    assert deadline.weekday() < 5
    assert deadline >= start


# --- SLA windows (AC-6.2.1) -----------------------------------------------------------------------


def test_ac_6_2_1_an_order_at_exactly_its_deadline_is_not_breached() -> None:
    policy = _policy(window=3, unit=WindowUnit.CALENDAR_DAYS)
    deadline = FRIDAY + timedelta(days=3)
    assert policy.deadline(FRIDAY) == deadline
    assert not policy.is_breached(FRIDAY, now=deadline)
    assert policy.is_breached(FRIDAY, now=deadline + timedelta(microseconds=1))
    assert not policy.is_breached(FRIDAY, now=deadline - timedelta(seconds=1))


def test_ac_6_2_1_business_day_boundary() -> None:
    policy = _policy(window=1)
    monday = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)
    assert not policy.is_breached(FRIDAY, now=monday)
    assert policy.is_breached(FRIDAY, now=monday + timedelta(seconds=1))
    # On Saturday and Sunday the order is inside its window.
    assert not policy.is_breached(FRIDAY, now=FRIDAY + timedelta(days=2))


def test_the_most_specific_policy_wins() -> None:
    org = _policy(window=10)
    shop = _policy(window=8, shop_id=SHOP)
    supplier = _policy(window=6, shop_id=SHOP, supplier="yun")
    method = _policy(window=4, shop_id=SHOP, supplier="yun", shipping_method="express")
    other = _policy(window=2, shop_id=OTHER_SHOP)
    policies = [org, shop, supplier, method, other]
    assert select_policy(policies, shop_id=SHOP) == shop
    assert select_policy(policies, shop_id=SHOP, supplier="yun") == supplier
    assert (
        select_policy(policies, shop_id=SHOP, supplier="yun", shipping_method="express") == method
    )
    assert select_policy([org, other], shop_id=SHOP) == org


def test_without_a_policy_the_generous_default_applies() -> None:
    policy = select_policy([], shop_id=SHOP)
    assert policy == default_policy()
    assert policy.window == DEFAULT_WINDOW_DAYS
    assert policy.unit is WindowUnit.BUSINESS_DAYS


def test_the_default_window_is_seven_business_days() -> None:
    # The owner's decision of 2026-10-23 (it was ten). Paid on Monday 2026-10-26: due seven
    # business days later, on Wednesday 2026-11-04, the weekend between not counted.
    paid = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)
    policy = default_policy()
    assert policy.window == 7  # the owner's number
    assert policy.deadline(paid) == datetime(2026, 11, 4, 15, 30, tzinfo=UTC)
    assert not policy.is_breached(paid, now=datetime(2026, 11, 4, 15, 29, tzinfo=UTC))
    assert policy.is_breached(paid, now=datetime(2026, 11, 4, 15, 31, tzinfo=UTC))


# --- Ship-by warnings (AC-6.9.1) -----------------------------------------------------------------


def test_ac_6_9_1_ship_by_warning_window_skips_holidays() -> None:
    tuesday = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    plain = BusinessCalendar()
    # Two business days before Tuesday: from Friday 00:00.
    assert ship_by_at_risk(
        tuesday,
        now=datetime(2026, 10, 23, 0, 0, tzinfo=UTC),
        warning_business_days=2,
        calendar=plain,
    )
    assert not ship_by_at_risk(
        tuesday,
        now=datetime(2026, 10, 22, 23, 59, tzinfo=UTC),
        warning_business_days=2,
        calendar=plain,
    )
    # With Monday a holiday the window opens a business day earlier (Thursday).
    holiday = BusinessCalendar(holidays=frozenset({date(2026, 10, 26)}))
    assert ship_by_at_risk(
        tuesday,
        now=datetime(2026, 10, 22, 0, 0, tzinfo=UTC),
        warning_business_days=2,
        calendar=holiday,
    )
    # Zero warning days: at risk from the ship-by moment.
    assert not ship_by_at_risk(
        tuesday, now=tuesday - timedelta(seconds=1), warning_business_days=0, calendar=plain
    )
    assert ship_by_at_risk(tuesday, now=tuesday, warning_business_days=0, calendar=plain)
