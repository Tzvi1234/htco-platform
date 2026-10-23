"""The circuit breaker's rules and the rate windows of the Action Gateway (slice 5.15,
`execution-engine.md` §4 step 6 and §9).

A breaker per key (`etsy:api`, `etsy:web`, later `browser:{shop}`) opens after `failures` failed
calls within `window`; while open every call is deferred until `cooldown` has passed; then one
probe goes through (half-open). A good probe closes it, a bad one opens it again. A probe that
never reports (its worker died) frees the half-open state after another cooldown.

Rate budgets count calls in aligned windows: each UTC second and each UTC day, so no second and
no day ever exceeds its budget. The state is plain values: in memory for tests and one process,
in Postgres for every worker (`htco_persistence.guards`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerPolicy:
    failures: int = 5
    window: timedelta = timedelta(seconds=60)
    cooldown: timedelta = timedelta(seconds=120)

    def __post_init__(self) -> None:
        if self.failures < 1:
            raise ValueError("a breaker opens after at least 1 failures")
        if self.window <= timedelta(0) or self.cooldown <= timedelta(0):
            raise ValueError("a breaker's window and cooldown are positive")


@dataclass(frozen=True)
class Breaker:
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    window_started_at: datetime | None = None
    opened_at: datetime | None = None
    probe_at: datetime | None = None


def admit(
    breaker: Breaker, policy: BreakerPolicy, *, now: datetime
) -> tuple[Breaker, datetime | None]:
    """The breaker after asking to call, and None (go) or when to try again."""
    if breaker.state is BreakerState.CLOSED:
        return breaker, None
    if breaker.state is BreakerState.OPEN:
        assert breaker.opened_at is not None  # noqa: S101 - an open breaker has opened
        reopen = breaker.opened_at + policy.cooldown
        if now < reopen:
            return breaker, reopen
        return Breaker(BreakerState.HALF_OPEN, breaker.failures, opened_at=breaker.opened_at,
                       probe_at=now), None  # fmt: skip
    assert breaker.probe_at is not None  # noqa: S101 - a half-open breaker has a probe
    free_at = breaker.probe_at + policy.cooldown
    if now < free_at:
        return breaker, free_at
    return Breaker(BreakerState.HALF_OPEN, breaker.failures, opened_at=breaker.opened_at,
                   probe_at=now), None  # fmt: skip


def record(breaker: Breaker, policy: BreakerPolicy, *, failed: bool, now: datetime) -> Breaker:
    """The breaker after a call's result. Successes in a closed breaker do not hide failures."""
    if breaker.state is BreakerState.HALF_OPEN:
        if failed:
            return Breaker(BreakerState.OPEN, policy.failures, opened_at=now)
        return Breaker()
    if breaker.state is BreakerState.OPEN or not failed:
        return breaker  # a late result of a call made before it opened changes nothing
    if breaker.window_started_at is None or now - breaker.window_started_at > policy.window:
        count, started = 1, now
    else:
        count, started = breaker.failures + 1, breaker.window_started_at
    if count >= policy.failures:
        return Breaker(BreakerState.OPEN, count, window_started_at=started, opened_at=now)
    return Breaker(BreakerState.CLOSED, count, window_started_at=started)


def second_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(microsecond=0)
    return start, start + timedelta(seconds=1)


def day_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
