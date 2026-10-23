"""The Action Gateway's guards on Postgres (slice 5.15): the token bucket, circuit breakers and
concurrency slots every worker shares, as the worker role, with a simulated clock."""

import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.execution.gateway import ActionOutcome, Channel
from htco_application.execution.guards import Guards, InlineRetry, RateBudget
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.marketplace import MarketplaceRateLimitedError
from htco_domain.guards import BreakerPolicy
from htco_persistence.actions import PostgresActionStore
from htco_persistence.database import Database
from htco_persistence.guards import PostgresBreakers, PostgresRateLimiter, PostgresSlots
from tests.application.test_action_gateway import office
from tests.execution_fakes import (
    Approvals,
    Capabilities,
    FakeChannel,
    Principals,
    Shops,
    real_catalog,
)
from tests.integration.test_action_store import SHOPS, ask, shop_of
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

START = datetime(2026, 10, 23, 23, 59, 58, tzinfo=UTC)  # two seconds before a new UTC day


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def workers() -> AsyncIterator[list[Database]]:
    """Two workers: two pools as the worker role."""
    pools = [Database(_url(), min_size=1, max_size=4, role="htco_worker") for _ in range(2)]
    for pool in pools:
        await pool.start()
    try:
        yield pools
    finally:
        for pool in pools:
            await pool.stop()


def together(count: int, work: Callable[[int], None]) -> None:
    barrier = threading.Barrier(count)

    def run(index: int) -> None:
        barrier.wait(timeout=10)
        work(index)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)


def moments(seconds: int, per_second: int) -> Iterator[datetime]:
    for i in range(seconds * per_second):
        yield START + timedelta(seconds=i / per_second)


@pytest.mark.ac("AC-5.15.1")
async def test_two_workers_four_shops__never_above_the_qps(workers: list[Database]) -> None:
    budget = RateBudget(f"etsy:test:{uuid.uuid4()}", per_second=3, per_day=1000)
    limiters = [PostgresRateLimiter(pool) for pool in workers]
    granted: list[datetime] = []
    guard = threading.Lock()

    def shop_worker(index: int) -> None:  # 2 workers x 4 shops, every quarter second
        limiter = limiters[index % 2]
        for moment in moments(4, 4):
            if limiter.acquire(budget, now=moment) is None:
                with guard:
                    granted.append(moment)

    together(8, shop_worker)

    per_second = Counter(moment.replace(microsecond=0) for moment in granted)
    assert set(per_second.values()) == {budget.per_second}  # full use, never more
    assert len(per_second) == 4


@pytest.mark.ac("AC-5.15.1")
async def test_two_workers_four_shops__never_above_the_qpd(workers: list[Database]) -> None:
    budget = RateBudget(f"etsy:test:{uuid.uuid4()}", per_second=7, per_day=7)
    limiters = [PostgresRateLimiter(pool) for pool in workers]
    refused: list[datetime | None] = []
    granted: list[datetime] = []
    guard = threading.Lock()

    def shop_worker(index: int) -> None:
        moment = START - timedelta(hours=1)
        for _ in range(5):
            retry_at = limiters[index % 2].acquire(budget, now=moment)
            with guard:
                (granted.append(moment) if retry_at is None else refused.append(retry_at))

    together(8, shop_worker)

    assert len(granted) == budget.per_day
    assert set(refused) == {datetime(2026, 10, 24, tzinfo=UTC)}  # the next UTC day
    assert limiters[0].acquire(budget, now=datetime(2026, 10, 24, 0, 0, 1, tzinfo=UTC)) is None


@pytest.mark.ac("AC-5.15.2")
async def test_breaker__shared_by_the_workers__one_probe_after_the_cooldown(
    workers: list[Database],
) -> None:
    key = f"etsy:test:{uuid.uuid4()}"
    policy = BreakerPolicy()
    first, second = (PostgresBreakers(pool, policy) for pool in workers)
    for i in range(5):
        (first if i % 2 else second).record(key, failed=True, now=START + timedelta(seconds=i))
    opened_at = START + timedelta(seconds=4)

    assert second.admit(key, now=opened_at + timedelta(seconds=1)) == opened_at + policy.cooldown
    admitted: list[datetime | None] = []
    guard = threading.Lock()

    def probe(index: int) -> None:
        answer = (first, second)[index % 2].admit(key, now=opened_at + policy.cooldown)
        with guard:
            admitted.append(answer)

    together(6, probe)

    assert admitted.count(None) == 1
    first.record(key, failed=False, now=opened_at + policy.cooldown + timedelta(seconds=1))
    assert second.admit(key, now=opened_at + policy.cooldown + timedelta(seconds=2)) is None


@pytest.mark.ac("AC-5.15.4")
async def test_slots__one_holder_at_a_time_across_workers(workers: list[Database]) -> None:
    key = f"shop:{uuid.uuid4()}:writes"
    slots = [PostgresSlots(pool) for pool in workers]
    holders = [uuid.uuid4() for _ in range(6)]
    won: list[uuid.UUID] = []
    guard = threading.Lock()

    def take(index: int) -> None:
        retry_at = slots[index % 2].acquire(
            key, limit=1, holder=holders[index], lease_until=START + timedelta(minutes=2),
            now=START,
        )  # fmt: skip
        if retry_at is None:
            with guard:
                won.append(holders[index])

    together(6, take)

    assert len(won) == 1
    loser = next(h for h in holders if h != won[0])
    lease = START + timedelta(minutes=3)

    def take_as_loser(seconds: int) -> datetime | None:
        return slots[1].acquire(
            key, limit=1, holder=loser, lease_until=lease, now=START + timedelta(seconds=seconds)
        )

    assert take_as_loser(5) == START + timedelta(minutes=2)
    slots[0].release(key, holder=won[0])
    assert take_as_loser(6) is None


async def test_slots__an_expired_lease_is_taken_over(workers: list[Database]) -> None:
    key = f"shop:{uuid.uuid4()}:writes"
    slots = PostgresSlots(workers[0])
    crashed, next_holder = uuid.uuid4(), uuid.uuid4()
    slots.acquire(key, limit=1, holder=crashed, lease_until=START, now=START - timedelta(minutes=1))

    lease = START + timedelta(minutes=2)

    def take(holder: uuid.UUID, seconds: int) -> datetime | None:
        return slots.acquire(
            key, limit=1, holder=holder, lease_until=lease, now=START + timedelta(seconds=seconds)
        )

    assert take(next_holder, 1) is None
    slots.release(key, holder=crashed)  # the crashed holder's late release frees nothing
    assert take(crashed, 2) is not None


async def test_deferred__stored_with_its_retry_at(admin: Database) -> None:
    tenant = Tenant(admin)
    shop_of(admin, tenant)
    registry = office()
    now = datetime.now(UTC).replace(microsecond=0)
    channel = FakeChannel(fail=MarketplaceRateLimitedError("429", retry_after_seconds=600))
    service = ActionGatewayService(
        GatewayParts(
            catalog=real_catalog(registry),
            registry=lambda: registry,
            store=PostgresActionStore(admin),
            principals=Principals(),
            shops=Shops(),
            approvals=Approvals(),
            adapters={Channel.API: channel},
            capabilities=Capabilities({(shop, "receipts_read") for shop in SHOPS.values()}),
            clock=lambda: now,
            guards=Guards(
                breakers=PostgresBreakers(admin, BreakerPolicy()),
                rates=PostgresRateLimiter(admin),
                slots=PostgresSlots(admin),
                retry=InlineRetry(),
            ),
        )
    )

    result = await service.submit(ask(tenant))

    assert result.outcome is ActionOutcome.DEFERRED
    assert result.retry_at == now + timedelta(seconds=600)
    assert (
        scalar(
            admin, "select state from app.action_executions where id = %s", [result.execution_id]
        )
        == "deferred"
    )
    assert scalar(
        admin, "select retry_at from app.action_executions where id = %s", [result.execution_id]
    ) == now + timedelta(seconds=600)
