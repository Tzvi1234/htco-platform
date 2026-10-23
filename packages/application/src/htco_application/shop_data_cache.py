"""A short in-memory cache for the dashboard's live shop data: single-flight, stale on error.

Etsy's limits are per app (about 10 requests a second and 10,000 a day for every shop together),
and its API Terms ask for minimal caching. So the dashboard reads through this cache, in process
memory only, never to disk or the database:

- **Fresh for `ttl`.** Within it every caller gets the cached value without a call.
- **Single flight.** When the value is due, the first caller loads it and every concurrent caller
  waits for that one load (one Etsy call however many tabs are open).
- **Stale on error.** If the load fails for a passing reason (Etsy unreachable, rate limited, the
  daily budget held back, an unreadable answer) and a value younger than `max_stale` exists, that
  value is served marked stale, with the time it was fetched. For `failure_cooldown` after a
  failure nobody calls Etsy again for that key.
- **Gone after `max_stale`.** Each value is dropped by a timer when its `max_stale` ends (and
  by a sweep on any access), so buyer data in order values never lives in memory longer than the
  receipts' `max_stale`, even when nobody asks again (Codex review of #50). `forget` drops a shop
  at once (it was disconnected or reconnected).

Keys carry the connection's generation: a reconnect never sees the previous connection's data.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from htco_application.marketplace import (
    MarketplaceError,
    MarketplaceRateLimitedError,
    MarketplaceResponseError,
    MarketplaceUnavailableError,
    QuotaDeferredError,
)

# After a failed load nobody calls Etsy again for this key for this long.
FAILURE_COOLDOWN = timedelta(seconds=30)

# Failures that pass on their own: a stale value may stand in while they last.
_PASSING = (
    MarketplaceUnavailableError,
    MarketplaceRateLimitedError,
    QuotaDeferredError,
    MarketplaceResponseError,
)


@dataclass(frozen=True)
class CachePolicy:
    """`ttl`: how long a value is served without a call. `max_stale`: how long it may stand in
    for a failed load, and how long it is kept at all."""

    ttl: timedelta
    max_stale: timedelta

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0) or self.max_stale < self.ttl:
            raise ValueError("a cache policy needs 0 < ttl <= max_stale")


@dataclass(frozen=True)
class CacheKey:
    shop_id: uuid.UUID
    generation: int
    resource: str


@dataclass(frozen=True)
class Fetched[ValueT]:
    value: ValueT
    fetched_at: datetime
    # True: the load failed for a passing reason and this older value stands in.
    stale: bool
    # True for the one caller whose call loaded the value (not its waiters, not cache hits).
    loaded: bool = False


@dataclass
class _Entry:
    value: Any
    fetched_at: datetime
    policy: CachePolicy


@dataclass
class _Failure:
    at: datetime
    error: MarketplaceError


class ShopDataCache:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        failure_cooldown: timedelta = FAILURE_COOLDOWN,
    ) -> None:
        self._clock = clock
        self._cooldown = failure_cooldown
        self._entries: dict[CacheKey, _Entry] = {}
        self._failures: dict[CacheKey, _Failure] = {}
        self._flights: dict[CacheKey, asyncio.Task[Any]] = {}
        # The expiry timer of each entry; it holds only the key, never the value.
        self._timers: dict[CacheKey, asyncio.TimerHandle] = {}
        # Grows with every `forget` of a shop: a load that started before must not store.
        self._epochs: dict[uuid.UUID, int] = {}

    async def get[ValueT](
        self, key: CacheKey, load: Callable[[], Awaitable[ValueT]], policy: CachePolicy
    ) -> Fetched[ValueT]:
        """The value for `key`: cached, loaded once for every concurrent caller, or stale.

        Raises the load's `MarketplaceError` when no usable value exists, or immediately (no
        call) during the cooldown after a failure.
        """

        async def undated() -> tuple[ValueT, datetime | None]:
            return await load(), None

        return await self.get_dated(key, undated, policy)

    async def get_dated[ValueT](
        self,
        key: CacheKey,
        load: Callable[[], Awaitable[tuple[ValueT, datetime | None]]],
        policy: CachePolicy,
    ) -> Fetched[ValueT]:
        """As `get`, for a value built from older data: the load also answers when that data was
        fetched (None: now), and freshness, staleness and expiry count from then (Codex review of
        #51)."""
        now = self._clock()
        self._sweep(now)
        entry = self._entries.get(key)
        if entry is not None and now - entry.fetched_at < policy.ttl:
            return Fetched(entry.value, entry.fetched_at, stale=False)
        failure = self._failures.get(key)
        if failure is not None and now - failure.at < self._cooldown:
            return self._stale_or_raise(key, entry, failure.error)
        flight = self._flights.get(key)
        leader = flight is None
        if flight is None:
            epoch = self._epochs.get(key.shop_id, 0)
            flight = asyncio.ensure_future(self._load(key, load, policy, epoch))
            self._flights[key] = flight
        try:
            value, fetched_at = await asyncio.shield(flight)
        except MarketplaceError as error:
            return self._stale_or_raise(key, entry, error)
        return Fetched(value, fetched_at, stale=False, loaded=leader)

    def forget(self, shop_id: uuid.UUID) -> None:
        """Drop everything cached for the shop (every generation) at once: entries, failures and
        their timers; loads still waiting for Etsy finish for their callers but store nothing, and
        new callers start their own (Codex review of #50)."""
        self._epochs[shop_id] = self._epochs.get(shop_id, 0) + 1
        for key in [k for k in self._entries if k.shop_id == shop_id]:
            self._drop(key)
        for key in [k for k in self._failures if k.shop_id == shop_id]:
            del self._failures[key]
        for key in [k for k in self._flights if k.shop_id == shop_id]:
            del self._flights[key]

    def __len__(self) -> int:
        return len(self._entries)

    async def _load[ValueT](
        self,
        key: CacheKey,
        load: Callable[[], Awaitable[tuple[ValueT, datetime | None]]],
        policy: CachePolicy,
        epoch: int,
    ) -> tuple[ValueT, datetime]:
        flight = asyncio.current_task()
        try:
            value, dated = await load()
        except MarketplaceError as error:
            if self._epochs.get(key.shop_id, 0) == epoch:
                self._failures[key] = _Failure(self._clock(), error)
                if not isinstance(error, _PASSING):
                    self._drop(key)
            raise
        finally:
            if self._flights.get(key) is flight:
                del self._flights[key]
        now = self._clock()
        fetched_at = now if dated is None else min(dated, now)
        if self._epochs.get(key.shop_id, 0) != epoch:
            return value, fetched_at  # forgotten meanwhile: answer the waiters, keep nothing
        self._drop(key)
        self._entries[key] = _Entry(value, fetched_at, policy)
        self._failures.pop(key, None)
        remaining = policy.max_stale - (now - fetched_at)
        self._timers[key] = asyncio.get_running_loop().call_later(
            max(0.0, remaining.total_seconds()), self._expire, key
        )
        return value, fetched_at

    def _expire(self, key: CacheKey) -> None:
        """The timer: drop the entry (a newer load cancels and replaces the timer)."""
        self._timers.pop(key, None)
        self._entries.pop(key, None)

    def _drop(self, key: CacheKey) -> None:
        self._entries.pop(key, None)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _stale_or_raise(
        self, key: CacheKey, entry: _Entry | None, error: MarketplaceError
    ) -> Fetched[Any]:
        current = self._entries.get(key)
        usable = current if current is not None else entry
        if usable is not None and isinstance(error, _PASSING):
            age = self._clock() - usable.fetched_at
            if age < usable.policy.max_stale:
                return Fetched(usable.value, usable.fetched_at, stale=True)
        raise error

    def _sweep(self, now: datetime) -> None:
        expired = [k for k, e in self._entries.items() if now - e.fetched_at >= e.policy.max_stale]
        for key in expired:
            self._drop(key)
        for key in [k for k, f in self._failures.items() if now - f.at >= self._cooldown]:
            del self._failures[key]
