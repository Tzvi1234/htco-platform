"""The circuit breaker's rules and the rate windows (slice 5.15): pure, with a simulated clock."""

from datetime import UTC, datetime, timedelta

import pytest

from htco_domain.guards import (
    Breaker,
    BreakerPolicy,
    BreakerState,
    admit,
    day_window,
    record,
    second_window,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
POLICY = BreakerPolicy()


def failures(count: int, *, every: timedelta = timedelta(seconds=10)) -> Breaker:
    breaker = Breaker()
    for i in range(count):
        breaker = record(breaker, POLICY, failed=True, now=NOW + i * every)
    return breaker


def test_policy__the_plans_figures() -> None:
    assert (POLICY.failures, POLICY.window, POLICY.cooldown) == (
        5,
        timedelta(seconds=60),
        timedelta(seconds=120),
    )


@pytest.mark.ac("AC-5.15.2")
def test_five_failures_in_60_seconds__open() -> None:
    assert failures(4).state is BreakerState.CLOSED
    opened = failures(5)

    assert opened.state is BreakerState.OPEN
    assert opened.opened_at == NOW + timedelta(seconds=40)


def test_five_failures_spread_over_more_than_60_seconds__stay_closed() -> None:
    assert failures(5, every=timedelta(seconds=20)).state is BreakerState.CLOSED


def test_successes__do_not_hide_failures_inside_the_window() -> None:
    breaker = Breaker()
    for i in range(5):
        breaker = record(breaker, POLICY, failed=True, now=NOW + timedelta(seconds=i))
        breaker = record(breaker, POLICY, failed=False, now=NOW + timedelta(seconds=i))
    assert breaker.state is BreakerState.OPEN


@pytest.mark.ac("AC-5.15.2")
def test_open__defers_until_the_cooldown_then_lets_one_probe_through() -> None:
    opened = failures(5)
    assert opened.opened_at is not None
    reopen = opened.opened_at + POLICY.cooldown

    _, retry_at = admit(opened, POLICY, now=reopen - timedelta(seconds=1))
    probing, first = admit(opened, POLICY, now=reopen)
    _, second = admit(probing, POLICY, now=reopen + timedelta(seconds=1))

    assert retry_at == reopen
    assert first is None
    assert probing.state is BreakerState.HALF_OPEN
    assert second is not None  # one probe at a time
    assert second > reopen


@pytest.mark.ac("AC-5.15.2")
def test_half_open__a_good_probe_closes_a_bad_one_opens_again() -> None:
    opened = failures(5)
    assert opened.opened_at is not None
    probing, _ = admit(opened, POLICY, now=opened.opened_at + POLICY.cooldown)
    later = opened.opened_at + POLICY.cooldown + timedelta(seconds=2)

    closed = record(probing, POLICY, failed=False, now=later)
    reopened = record(probing, POLICY, failed=True, now=later)

    assert closed == Breaker()
    assert reopened.state is BreakerState.OPEN
    assert reopened.opened_at == later


def test_half_open__a_probe_that_never_reports__lets_another_through_after_the_cooldown() -> None:
    opened = failures(5)
    assert opened.opened_at is not None
    start = opened.opened_at + POLICY.cooldown
    probing, _ = admit(opened, POLICY, now=start)

    again, retry_at = admit(probing, POLICY, now=start + POLICY.cooldown)

    assert retry_at is None
    assert again.probe_at == start + POLICY.cooldown


def test_windows__aligned_seconds_and_utc_days() -> None:
    moment = datetime(2026, 10, 23, 23, 59, 59, 700_000, tzinfo=UTC)

    assert second_window(moment) == (
        datetime(2026, 10, 23, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 24, 0, 0, 0, tzinfo=UTC),
    )
    assert day_window(moment) == (
        datetime(2026, 10, 23, tzinfo=UTC),
        datetime(2026, 10, 24, tzinfo=UTC),
    )


def test_policy__refuses_nonsense() -> None:
    with pytest.raises(ValueError, match="failures"):
        BreakerPolicy(failures=0)
