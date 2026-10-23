"""The Action Gateway's guards (slice 5.15, `execution-engine.md` §4 step 6 and §9).

Before each channel call the gateway asks, in this order:

1. the **circuit breaker** of the binding's `breaker_key` (`etsy:api`): open means `DEFERRED`;
2. the **rate limiter** for the action's `rate_limit_key` (`etsy:app`): one budget per second and
   per UTC day shared by every worker and shop, so the app never exceeds Etsy's QPS or QPD;
3. once per execution, the **concurrency slot** of the action's `concurrency_key`
   (`shop:{shop_id}:writes`): two writes to one shop never run at the same time.

Each answers None (go) or when to try again; the gateway then returns `DEFERRED` with
`retry_at`. `InlineRetry` bounds the retries inside one attempt: a 429 waits for `Retry-After`
when it is short, a 5xx or a lost connection is retried for reads only (a write that may have
been applied is never sent again blindly), any other 4xx is never retried.

The shared state is Postgres (`htco_persistence.guards`); the in-memory guards here serve tests
and a single process. Standard library only: the plan's PyBreaker, aiolimiter and Tenacity are
replaced by these small rules (owner-reviewed deviation, `execution-engine.md` §9).
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from htco_domain.guards import Breaker, BreakerPolicy, admit, day_window, record, second_window


@dataclass(frozen=True)
class RateBudget:
    key: str
    per_second: int
    per_day: int

    def __post_init__(self) -> None:
        if self.per_second < 1:
            raise ValueError(f"rate budget {self.key!r}: per_second is at least 1")
        if self.per_day < self.per_second:
            raise ValueError(f"rate budget {self.key!r}: per_day is at least per_second")


class CircuitBreakers(Protocol):
    def admit(self, key: str, *, now: datetime) -> datetime | None:
        """None: call now; otherwise when to try again."""
        ...

    def record(self, key: str, *, failed: bool, now: datetime) -> None: ...


class RateLimiter(Protocol):
    def acquire(self, budget: RateBudget, *, now: datetime, cost: int = 1) -> datetime | None:
        """Spend `cost` calls of this second's and this day's budget, or say when it refills."""
        ...


class ConcurrencyLimiter(Protocol):
    def acquire(
        self, key: str, *, limit: int, holder: uuid.UUID, lease_until: datetime, now: datetime
    ) -> datetime | None:
        """Take one of `limit` slots until `lease_until`, or say when the first one frees."""
        ...

    def release(self, key: str, *, holder: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class InlineRetry:
    max_attempts: int = 3  # calls inside one attempt of the action
    backoff_seconds: tuple[float, ...] = (0.5, 1.0)  # before the 2nd and 3rd call
    max_inline_wait: timedelta = timedelta(seconds=5)  # a longer Retry-After defers instead
    unknown_rate_wait: timedelta = timedelta(seconds=60)  # a 429 without Retry-After

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or len(self.backoff_seconds) < self.max_attempts - 1:
            raise ValueError("inline retries need one backoff per retry")

    def backoff(self, attempt: int) -> float:
        """The wait before call `attempt + 1`."""
        return self.backoff_seconds[attempt - 1]


@dataclass(frozen=True)
class Guards:
    breakers: CircuitBreakers
    rates: RateLimiter
    slots: ConcurrencyLimiter
    retry: InlineRetry = InlineRetry()
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    slot_limit: int = 1  # one write per shop at a time
    slot_margin: timedelta = timedelta(seconds=60)  # a lease outlives the action's timeout


# --- in memory (tests, one process) ---------------------------------------------------------


@dataclass
class MemoryBreakers:
    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    breakers: dict[str, Breaker] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, key: str, *, now: datetime) -> datetime | None:
        with self._lock:
            self.breakers[key], retry_at = admit(self.breakers.get(key, Breaker()), self.policy,
                                                 now=now)  # fmt: skip
            return retry_at

    def record(self, key: str, *, failed: bool, now: datetime) -> None:
        with self._lock:
            self.breakers[key] = record(
                self.breakers.get(key, Breaker()), self.policy, failed=failed, now=now
            )


@dataclass
class MemoryRateLimiter:
    used: dict[tuple[str, str, datetime], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, budget: RateBudget, *, now: datetime, cost: int = 1) -> datetime | None:
        second, next_second = second_window(now)
        day, next_day = day_window(now)
        keys = ((budget.key, "second", second), (budget.key, "day", day))
        with self._lock:
            if self.used.get(keys[1], 0) + cost > budget.per_day:
                return next_day
            if self.used.get(keys[0], 0) + cost > budget.per_second:
                return next_second
            for key in keys:
                self.used[key] = self.used.get(key, 0) + cost
            return None


@dataclass
class MemorySlots:
    held: dict[tuple[str, int], tuple[uuid.UUID, datetime]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(
        self, key: str, *, limit: int, holder: uuid.UUID, lease_until: datetime, now: datetime
    ) -> datetime | None:
        with self._lock:
            for slot in range(limit):
                current = self.held.get((key, slot))
                if current is None or current[1] <= now:
                    self.held[key, slot] = (holder, lease_until)
                    return None
            return min(self.held[key, slot][1] for slot in range(limit))

    def release(self, key: str, *, holder: uuid.UUID) -> None:
        with self._lock:
            for slot_key, (current, _) in list(self.held.items()):
                if slot_key[0] == key and current == holder:
                    del self.held[slot_key]
