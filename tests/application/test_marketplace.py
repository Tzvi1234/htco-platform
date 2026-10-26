"""The marketplace port's value types (slice 4.1): money and pages."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from htco_application.kernel import Page
from htco_application.marketplace import MarketplaceMoney, QuotaSnapshot


@pytest.mark.parametrize(
    ("amount", "divisor", "expected"),
    [
        (2599, 100, Decimal("25.99")),
        (0, 100, Decimal("0.00")),
        (-150, 100, Decimal("-1.50")),
        (1999, 1000, Decimal("1.999")),
        (5, 1, Decimal(5)),
    ],
)
def test_money__as_decimal__divides_exactly(amount: int, divisor: int, expected: Decimal) -> None:
    money = MarketplaceMoney(amount=amount, divisor=divisor, currency_code="USD")

    assert money.as_decimal() == expected


@pytest.mark.parametrize("divisor", [0, -100])
def test_money__non_positive_divisor__is_refused(divisor: int) -> None:
    with pytest.raises(ValueError, match="divisor"):
        MarketplaceMoney(amount=100, divisor=divisor, currency_code="USD")


@pytest.mark.parametrize("currency", ["", "usd", "US", "USDX"])
def test_money__currency_must_be_iso_4217(currency: str) -> None:
    with pytest.raises(ValueError, match="currency"):
        MarketplaceMoney(amount=100, divisor=100, currency_code=currency)


@pytest.mark.parametrize(
    ("count", "offset", "returned", "expected"),
    [
        (0, 0, 0, None),
        (2, 0, 2, None),
        (30, 0, 25, 25),
        (30, 25, 5, None),
        (51, 25, 25, 50),
    ],
)
def test_page__next_offset__points_past_the_returned_items(
    count: int, offset: int, returned: int, expected: int | None
) -> None:
    page = Page(items=tuple(range(returned)), total_count=count, offset=offset)

    assert page.next_offset == expected


def test_quota_snapshot__below_floor__compares_remaining_today() -> None:
    snapshot = QuotaSnapshot(
        limit_per_second=10,
        remaining_this_second=9,
        limit_per_day=10_000,
        remaining_today=499,
        observed_at=datetime(2026, 10, 15, tzinfo=UTC),
    )

    assert snapshot.is_below(500)
    assert not snapshot.is_below(499)
