"""The dashboard's per-shop cache: fresh for a while, one load for concurrent callers, stale on a
passing failure, forgotten after `max_stale` or on demand (Etsy's per-app rate limits)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.marketplace import (
    MarketplaceAuthError,
    MarketplaceError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceUnavailableError,
    QuotaDeferredError,
)
from htco_application.shop_data_cache import CacheKey, CachePolicy, ShopDataCache

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
SHOP = uuid.UUID(int=1)
KEY = CacheKey(SHOP, 1, "profile")
POLICY = CachePolicy(ttl=timedelta(minutes=5), max_stale=timedelta(hours=1))


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Loader:
    """Counts loads; answers with the next scripted value or error."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> object:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_policy__ttl_must_be_positive_and_within_max_stale() -> None:
    with pytest.raises(ValueError, match="ttl"):
        CachePolicy(ttl=timedelta(0), max_stale=timedelta(hours=1))
    with pytest.raises(ValueError, match="ttl"):
        CachePolicy(ttl=timedelta(hours=2), max_stale=timedelta(hours=1))


async def test_get__within_ttl__served_without_a_second_load() -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    load = Loader("v1", "v2")

    first = await cache.get(KEY, load, POLICY)
    clock.advance(minutes=4)
    second = await cache.get(KEY, load, POLICY)

    assert (first.value, first.stale, first.loaded) == ("v1", False, True)
    assert (second.value, second.stale, second.loaded) == ("v1", False, False)
    assert second.fetched_at == START
    assert load.calls == 1


async def test_get__after_ttl__loads_again() -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    load = Loader("v1", "v2")

    await cache.get(KEY, load, POLICY)
    clock.advance(minutes=5)
    again = await cache.get(KEY, load, POLICY)

    assert again.value == "v2"
    assert again.fetched_at == START + timedelta(minutes=5)
    assert load.calls == 2


async def test_get__ten_concurrent_callers__one_load_one_leader() -> None:
    cache = ShopDataCache(clock=Clock())
    load = Loader("v1")
    load.gate = asyncio.Event()

    pending = [asyncio.ensure_future(cache.get(KEY, load, POLICY)) for _ in range(10)]
    await asyncio.sleep(0)
    load.gate.set()
    results = await asyncio.gather(*pending)

    assert load.calls == 1
    assert {r.value for r in results} == {"v1"}
    assert sum(r.loaded for r in results) == 1


async def test_get__keys_are_separate_per_resource_and_generation() -> None:
    cache = ShopDataCache(clock=Clock())
    load = Loader("v")

    await cache.get(KEY, load, POLICY)
    await cache.get(CacheKey(SHOP, 2, "profile"), load, POLICY)
    await cache.get(CacheKey(SHOP, 1, "listings"), load, POLICY)

    assert load.calls == 3


@pytest.mark.parametrize(
    "error",
    [
        MarketplaceUnavailableError("down", status=503),
        MarketplaceRateLimitedError("slow down", retry_after_seconds=5),
        QuotaDeferredError("budget", remaining_today=10, floor=500),
    ],
)
async def test_get__passing_failure_with_a_cached_value__stale_value_with_its_time(
    error: MarketplaceError,
) -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    load = Loader("v1", error)

    await cache.get(KEY, load, POLICY)
    clock.advance(minutes=10)
    stale = await cache.get(KEY, load, POLICY)

    assert (stale.value, stale.stale, stale.loaded) == ("v1", True, False)
    assert stale.fetched_at == START


async def test_get__failure_without_a_cached_value__raises() -> None:
    cache = ShopDataCache(clock=Clock())

    with pytest.raises(MarketplaceUnavailableError):
        await cache.get(KEY, Loader(MarketplaceUnavailableError("down")), POLICY)


async def test_get__after_a_failure__no_call_during_the_cooldown() -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock, failure_cooldown=timedelta(seconds=30))
    load = Loader(MarketplaceUnavailableError("down"), "v1")

    with pytest.raises(MarketplaceUnavailableError):
        await cache.get(KEY, load, POLICY)
    clock.advance(seconds=10)
    with pytest.raises(MarketplaceUnavailableError):
        await cache.get(KEY, load, POLICY)
    assert load.calls == 1

    clock.advance(seconds=20)
    recovered = await cache.get(KEY, load, POLICY)
    assert recovered.value == "v1"
    assert load.calls == 2


async def test_get__stale_value_older_than_max_stale__not_served_and_dropped() -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    load = Loader("v1", MarketplaceUnavailableError("down"))

    await cache.get(KEY, load, POLICY)
    clock.advance(hours=1)
    with pytest.raises(MarketplaceUnavailableError):
        await cache.get(KEY, load, POLICY)

    assert len(cache) == 0


@pytest.mark.parametrize(
    "error",
    [MarketplaceAuthError("revoked", status=401), MarketplacePermissionError("scope", status=403)],
)
async def test_get__lasting_failure__raises_and_drops_the_cached_value(
    error: MarketplaceError,
) -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    load = Loader("v1", error)

    await cache.get(KEY, load, POLICY)
    clock.advance(minutes=6)
    with pytest.raises(type(error)):
        await cache.get(KEY, load, POLICY)

    assert len(cache) == 0


async def test_forget__drops_every_generation_of_the_shop_only() -> None:
    cache = ShopDataCache(clock=Clock())
    other = CacheKey(uuid.UUID(int=2), 1, "profile")
    load = Loader("v")
    await cache.get(KEY, load, POLICY)
    await cache.get(CacheKey(SHOP, 2, "receipts"), load, POLICY)
    await cache.get(other, load, POLICY)

    cache.forget(SHOP)

    assert len(cache) == 1
    await cache.get(other, load, POLICY)
    assert load.calls == 3


async def test_get__entries_past_max_stale_are_swept_on_any_access() -> None:
    clock = Clock()
    cache = ShopDataCache(clock=clock)
    await cache.get(KEY, Loader("old"), POLICY)

    clock.advance(hours=2)
    await cache.get(CacheKey(uuid.UUID(int=2), 1, "profile"), Loader("new"), POLICY)

    assert len(cache) == 1
