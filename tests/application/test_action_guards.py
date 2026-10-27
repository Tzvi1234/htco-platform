"""Circuit breakers, rate and concurrency limits and bounded retries at the Action Gateway
(slice 5.15, `execution-engine.md` §4 step 6). AC-5.15.1 to AC-5.15.4 with the in-memory guards
and a simulated clock; the Postgres guards shared by several workers are in tests/integration."""

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from htco_application.execution.contracts import (
    ActionCatalog,
    ActionCatalogError,
    ChannelBinding,
    ChannelOutcome,
)
from htco_application.execution.gateway import ActionOutcome, ActionRequest, Channel
from htco_application.execution.guards import (
    Guards,
    InlineRetry,
    MemoryBreakers,
    MemoryRateLimiter,
    MemorySlots,
    RateBudget,
)
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.marketplace import (
    MarketplaceRateLimitedError,
    MarketplaceRequestError,
    MarketplaceUnavailableError,
    QuotaDeferredError,
)
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.guards import BreakerPolicy
from tests.application.test_action_gateway import (
    ORG,
    SHOP,
    USER_ACTOR,
    World,
    ask,
    office,
)
from tests.execution_fakes import (
    Approvals,
    Capabilities,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
SHOPS = [uuid.UUID(f"7a300000-0000-0000-0000-00000000001{i}") for i in range(4)]


@dataclass
class Clock:
    now: datetime = START
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


@dataclass
class Scripted:
    """A channel that answers each call from a script (an exception or an output), then `rest`."""

    clock: Clock
    script: list[Exception | dict[str, Any]] = field(default_factory=list)
    rest: dict[str, Any] = field(default_factory=lambda: {"receipts": [], "count": 0})
    channel: Channel = Channel.API
    calls: list[datetime] = field(default_factory=list)
    hold: anyio.Event | None = None
    entered: anyio.Event = field(default_factory=anyio.Event)
    running: int = 0
    most_at_once: int = 0

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:  # noqa: ARG002
        self.calls.append(self.clock())
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            self.entered.set()
            if self.hold is not None:
                await self.hold.wait()
            answer = self.script.pop(0) if self.script else self.rest
            if isinstance(answer, Exception):
                raise answer
            return ChannelOutcome(output=answer)
        finally:
            self.running -= 1

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return True


def guards(clock: Clock, **changes: Any) -> Guards:
    values: dict[str, Any] = {
        "breakers": MemoryBreakers(BreakerPolicy()),
        "rates": MemoryRateLimiter(),
        "slots": MemorySlots(),
        "retry": InlineRetry(),
        "sleep": clock.sleep,
    } | changes
    return Guards(**values)


def gateway(  # noqa: PLR0913 - a test world with optional parts
    channel: Scripted,
    clock: Clock,
    shared: Guards,
    *,
    budget: RateBudget | None = None,
    store: MemoryActionStore | None = None,
    timeout: timedelta | None = None,
) -> ActionGatewayService:
    registry = office()
    # A generous budget unless the test is about the budget.
    budget = budget or RateBudget("etsy:app", per_second=1000, per_day=100_000)
    # A scripted channel runs any recipe; the real API channel gets the catalog as declared.
    built = getattr(channel, "recipes", None) is None
    catalog = replace(real_catalog(registry, built=built), rate_budgets={budget.key: budget})
    if timeout is not None:
        actions = {n: replace(d, timeout=timeout) for n, d in catalog.actions.items()}
        catalog = replace(catalog, actions=actions)
    world = World()
    return ActionGatewayService(
        GatewayParts(
            catalog=catalog,
            registry=lambda: registry,
            store=store or MemoryActionStore(),
            principals=world.principals,
            shops=Shops({SHOP: ORG, **dict.fromkeys(SHOPS, ORG)}),
            approvals=Approvals(),
            adapters={Channel.API: channel},
            capabilities=Capabilities(
                {(s, c) for s in [SHOP, *SHOPS] for c in ("receipts_read", "shop_details")}
            ),
            clock=clock,
            guards=shared,
        )
    )


def write(key: str, shop: uuid.UUID = SHOP) -> ActionRequest:
    return ask(
        "etsy.inventory.update",
        actor=USER_ACTOR,
        key=key,
        shop_id=shop,
        owner_trigger=True,
        input={"listing_id": 1, "products": []},
    )


def per_second(calls: Sequence[datetime]) -> Counter[datetime]:
    return Counter(call.replace(microsecond=0) for call in calls)


def test_catalog__the_real_etsy_budget_is_declared() -> None:
    catalog = real_catalog(built=True)

    budget = catalog.rate_budgets["etsy:app"]
    assert budget.per_second >= 1
    assert budget.per_day >= budget.per_second
    for definition in catalog.actions.values():
        assert definition.rate_limit_key in catalog.rate_budgets
        assert all(b.breaker_key == "etsy:api" for b in definition.bindings)


@pytest.mark.parametrize(
    ("limits", "problem"),
    [
        ([], "no rate budget 'etsy:app'"),
        ([{"key": "etsy:app", "per_second": 0, "per_day": 10}], "per_second"),
        ([{"key": "etsy:app", "per_second": 5, "per_day": 2}], "per_day"),
    ],
)
def test_catalog__rate_budgets_are_checked(limits: list[dict[str, Any]], problem: str) -> None:
    registry = office()
    document = {
        "tool": "etsy.shop.read",
        "approval_policy": "R0",
        "verify": "schema",
        "timeout_seconds": 30,
        "input_schema": "in.json",
        "output_schema": "out.json",
        "rate_limit_key": "etsy:app",
        "bindings": [{"channel": "api", "recipe": "get_shop", "breaker_key": "etsy:api"}],
    }
    schemas = {"in.json": {"type": "object"}, "out.json": {"type": "object"}}

    with pytest.raises(ActionCatalogError, match=problem):
        ActionCatalog.from_documents(
            [document], tools=registry.tools, schemas=schemas, rate_limits=limits
        )


# --- AC-5.15.1: the app-wide budget across workers and shops --------------------------------


@pytest.mark.ac("AC-5.15.1")
async def test_two_workers_four_shops__never_above_the_qps_or_the_qpd() -> None:
    clock = Clock()
    channel = Scripted(clock)
    shared = guards(clock)  # one limiter: the Postgres bucket every worker shares
    budget = RateBudget("etsy:app", per_second=3, per_day=20)
    store = MemoryActionStore()
    workers = [gateway(channel, clock, shared, budget=budget, store=store) for _ in range(2)]
    pending = [ask(key=f"receipts:{shop}:{n}", shop_id=shop) for shop in SHOPS for n in range(10)]
    retry_at: dict[str, datetime] = {}
    done: set[str] = set()

    for tick in range(2 * 24 * 3600 // 15):  # two simulated days in 15-second steps
        clock.now = START + timedelta(seconds=15 * tick)
        for request in pending:
            if (
                request.idempotency_key in done
                or retry_at.get(request.idempotency_key, START) > clock.now
            ):
                continue
            worker = workers[hash(request.idempotency_key) % 2]
            result = await worker.submit(request)
            if result.outcome is ActionOutcome.SUCCEEDED:
                done.add(request.idempotency_key)
            else:
                assert result.outcome is ActionOutcome.DEFERRED
                assert result.retry_at is not None
                assert result.retry_at > clock.now
                retry_at[request.idempotency_key] = result.retry_at
        if len(done) == len(pending):
            break

    assert len(done) == len(pending)  # every action ran once the budget allowed it
    assert max(per_second(channel.calls).values()) <= budget.per_second
    days = Counter(call.date() for call in channel.calls)
    assert max(days.values()) <= budget.per_day
    assert days[START.date()] == budget.per_day  # the first day's budget was used, not exceeded


# --- AC-5.15.2: circuit breaker ------------------------------------------------------------


@pytest.mark.ac("AC-5.15.2")
async def test_five_failures__open_the_breaker__deferred_then_one_probe_closes_it() -> None:
    clock = Clock()
    down = [MarketplaceUnavailableError("etsy down", status=503) for _ in range(5)]
    channel = Scripted(clock, script=list(down))
    shared = guards(clock, retry=InlineRetry(max_attempts=1))
    service = gateway(channel, clock, shared)

    for n in range(5):
        failed = await service.submit(ask(key=f"receipts:{n}"))
        assert failed.outcome is ActionOutcome.FAILED
        clock.now += timedelta(seconds=5)
    deferred = await service.submit(ask(key="receipts:5"))

    assert deferred.outcome is ActionOutcome.DEFERRED
    assert deferred.error_code == "breaker_open"
    assert deferred.retry_at == START + timedelta(seconds=20) + timedelta(seconds=120)
    assert len(channel.calls) == 5  # the open breaker sent nothing

    assert deferred.retry_at is not None
    clock.now = deferred.retry_at
    probe = await service.submit(ask(key="receipts:5"))  # the same key, resubmitted
    after = await service.submit(ask(key="receipts:6"))

    assert probe.outcome is ActionOutcome.SUCCEEDED
    assert after.outcome is ActionOutcome.SUCCEEDED
    assert len(channel.calls) == 7


@pytest.mark.ac("AC-5.15.2")
async def test_half_open__only_one_call_goes_through_at_a_time() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceUnavailableError("down", status=503)] * 5)
    shared = guards(clock, retry=InlineRetry(max_attempts=1))
    service = gateway(channel, clock, shared)
    for n in range(5):
        await service.submit(ask(key=f"receipts:{n}"))
    clock.now += timedelta(seconds=121)
    channel.hold, channel.entered = anyio.Event(), anyio.Event()
    results = {}

    async def probe() -> None:
        results["probe"] = await service.submit(ask(key="receipts:probe"))

    async with anyio.create_task_group() as group:
        group.start_soon(probe)
        await channel.entered.wait()
        results["other"] = await service.submit(ask(key="receipts:other"))
        channel.hold.set()

    assert results["probe"].outcome is ActionOutcome.SUCCEEDED
    assert results["other"].outcome is ActionOutcome.DEFERRED
    assert results["other"].error_code == "breaker_open"


async def test_breaker__per_key__another_key_is_unaffected() -> None:
    clock = Clock()
    breakers = MemoryBreakers(BreakerPolicy())
    for _ in range(5):
        breakers.record("etsy:api", failed=True, now=clock.now)

    assert breakers.admit("etsy:api", now=clock.now) is not None
    assert breakers.admit("etsy:web", now=clock.now) is None


# --- AC-5.15.3: what is retried ---------------------------------------------------------------


@pytest.mark.ac("AC-5.15.3")
async def test_a_4xx__never_retried_and_not_a_breaker_failure() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceRequestError("bad", status=400)] * 6)
    service = gateway(channel, clock, guards(clock))

    for n in range(6):
        result = await service.submit(ask(key=f"receipts:{n}"))
        assert (result.outcome, result.attempts) == (ActionOutcome.FAILED, 1)
    healthy = await service.submit(ask(key="receipts:ok"))

    assert len(channel.calls) == 7
    assert clock.slept == []
    assert healthy.outcome is ActionOutcome.SUCCEEDED  # the breaker stayed closed


@pytest.mark.ac("AC-5.15.3")
async def test_a_429__waits_for_retry_after_then_runs() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceRateLimitedError("429", retry_after_seconds=2)])
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert (result.outcome, result.attempts) == (ActionOutcome.SUCCEEDED, 2)
    assert clock.slept == [2]
    assert channel.calls == [START, START + timedelta(seconds=2)]


@pytest.mark.ac("AC-5.15.3")
@pytest.mark.parametrize(("retry_after", "wait"), [(300, 300), (None, 60)])
async def test_a_429_with_a_long_or_unknown_wait__deferred_until_then(
    retry_after: float | None, wait: int
) -> None:
    clock = Clock()
    channel = Scripted(
        clock, script=[MarketplaceRateLimitedError("429", retry_after_seconds=retry_after)]
    )
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert result.outcome is ActionOutcome.DEFERRED
    assert result.error_code == "rate_limited"
    assert result.retry_at == START + timedelta(seconds=wait)
    assert len(channel.calls) == 1


async def test_the_daily_quota_floor__deferred_to_the_next_day() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[QuotaDeferredError("low", remaining_today=10, floor=500)])
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DEFERRED, "rate_limited")
    assert result.retry_at == datetime(2026, 10, 24, tzinfo=UTC)


async def test_a_5xx_read__retried_a_bounded_number_of_times_with_backoff() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceUnavailableError("down", status=502)] * 2)
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert (result.outcome, result.attempts) == (ActionOutcome.SUCCEEDED, 3)
    assert clock.slept == list(InlineRetry().backoff_seconds)


async def test_a_5xx_read__gives_up_after_the_last_attempt() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceUnavailableError("down", status=500)] * 9)
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert (result.outcome, result.attempts) == (ActionOutcome.FAILED, 3)
    assert len(channel.calls) == 3


async def test_a_5xx_write__never_retried_inside_the_attempt() -> None:
    """A write that may have been applied is not sent again blindly; the job retries it later
    with the same idempotency key, and verification decides."""
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceUnavailableError("down", status=503)])
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(write("inventory:1"))

    assert (result.outcome, result.attempts) == (ActionOutcome.FAILED, 1)
    assert clock.slept == []


async def test_every_call_of_a_retry__counts_against_the_budget() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceUnavailableError("down", status=502)] * 2)
    budget = RateBudget("etsy:app", per_second=2, per_day=2)
    service = gateway(channel, clock, guards(clock), budget=budget)

    result = await service.submit(ask())

    assert result.outcome is ActionOutcome.DEFERRED
    assert result.error_code == "rate_limited"
    assert len(channel.calls) == 2


# --- AC-5.15.4: the per-shop write slot -------------------------------------------------------


@pytest.mark.ac("AC-5.15.4")
async def test_two_writes_to_one_shop__never_at_the_same_time() -> None:
    clock = Clock()
    channel = Scripted(clock, rest={"listing_id": 1}, hold=anyio.Event())
    shared = guards(clock)
    store = MemoryActionStore()
    first_worker = gateway(channel, clock, shared, store=store)
    second_worker = gateway(channel, clock, shared, store=store)
    results = {}

    async def first() -> None:
        results["first"] = await first_worker.submit(write("inventory:1"))

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        await channel.entered.wait()
        results["second"] = await second_worker.submit(write("inventory:2"))
        assert channel.hold is not None
        channel.hold.set()
    again = await second_worker.submit(write("inventory:2"))

    assert results["first"].outcome is ActionOutcome.SUCCEEDED
    assert results["second"].outcome is ActionOutcome.DEFERRED
    assert results["second"].error_code == "slot_busy"
    assert results["second"].retry_at is not None
    assert again.outcome is ActionOutcome.SUCCEEDED  # the slot was released
    assert channel.most_at_once == 1


@pytest.mark.ac("AC-5.15.4")
async def test_writes_to_two_shops__run_side_by_side() -> None:
    clock = Clock()
    channel = Scripted(clock, rest={"listing_id": 1}, hold=anyio.Event())
    service = gateway(channel, clock, guards(clock))
    results = {}

    async def run(name: str, shop: uuid.UUID) -> None:
        results[name] = await service.submit(write(f"inventory:{name}", shop))

    async with anyio.create_task_group() as group:
        group.start_soon(run, "a", SHOPS[0])
        group.start_soon(run, "b", SHOPS[1])
        while channel.running < 2:
            await anyio.lowlevel.checkpoint()
        assert channel.hold is not None
        channel.hold.set()

    assert {r.outcome for r in results.values()} == {ActionOutcome.SUCCEEDED}
    assert channel.most_at_once == 2


async def test_slot__released_when_the_channel_fails() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[RuntimeError("boom")], rest={"listing_id": 1})
    service = gateway(channel, clock, guards(clock))

    failed = await service.submit(write("inventory:1"))
    after = await service.submit(write("inventory:2"))

    assert failed.outcome is ActionOutcome.FAILED
    assert after.outcome is ActionOutcome.SUCCEEDED


async def test_slot__an_expired_lease_is_taken_over() -> None:
    slots = MemorySlots()
    holder, other = uuid.uuid4(), uuid.uuid4()

    assert slots.acquire("k", limit=1, holder=holder, lease_until=START + timedelta(minutes=1),
                         now=START) is None  # fmt: skip
    busy = slots.acquire("k", limit=1, holder=other, lease_until=START + timedelta(minutes=2),
                         now=START)  # fmt: skip
    taken = slots.acquire("k", limit=1, holder=other, lease_until=START + timedelta(minutes=3),
                          now=START + timedelta(minutes=1))  # fmt: skip

    assert busy == START + timedelta(minutes=1)
    assert taken is None


# --- the record -------------------------------------------------------------------------------


async def test_deferred__recorded_resumable_and_rechecked_on_resubmission() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceRateLimitedError("429", retry_after_seconds=90)])
    store = MemoryActionStore()
    service = gateway(channel, clock, guards(clock), store=store)

    deferred = await service.submit(ask())
    row = store.only()
    assert row.state is ActionState.DEFERRED
    assert row.steps[-1].reason == "rate_limited"
    # Not a result yet, but a status change: one `actions.deferred.v1`, never `executed` (5.18).
    deferral = row.steps[-1].event
    assert deferral is not None
    assert deferral.payload["event_type"] == "actions.deferred.v1"
    clock.now += timedelta(seconds=90)
    done = await service.submit(ask())

    assert deferred.outcome is ActionOutcome.DEFERRED
    assert done.outcome is ActionOutcome.SUCCEEDED
    assert done.execution_id == deferred.execution_id
    assert store.only().steps[-1].path[:2] == (ActionState.DEFERRED, ActionState.AUTHORIZED)


async def test_deferred__the_same_key_for_another_request__idempotency_conflict() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceRateLimitedError("429", retry_after_seconds=90)])
    store = MemoryActionStore()
    service = gateway(channel, clock, guards(clock), store=store)
    await service.submit(ask())
    request = ask(actor=ActorRef(ActorKind.AUTOMATION, "no-such-automation"))
    clock.now += timedelta(seconds=90)

    # The same key from another actor is another request: refused, the deferred row is kept.
    result = await service.submit(request)
    resumed = await service.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "idempotency_conflict")
    assert resumed.outcome is ActionOutcome.SUCCEEDED


@pytest.mark.ac("AC-5.15.3")
async def test_deferred__resubmitted_before_retry_at__deferred_again_without_a_call() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[MarketplaceRateLimitedError("429", retry_after_seconds=300)])
    service = gateway(channel, clock, guards(clock))

    first = await service.submit(ask())
    clock.now += timedelta(seconds=299)
    early = await service.submit(ask())
    clock.now += timedelta(seconds=1)
    due = await service.submit(ask())

    assert early.outcome is ActionOutcome.DEFERRED
    assert early.retry_at == first.retry_at == START + timedelta(seconds=300)
    assert due.outcome is ActionOutcome.SUCCEEDED
    assert len(channel.calls) == 2  # the 429 and the call after Retry-After, nothing between


@pytest.mark.ac("AC-5.15.4")
async def test_a_write_past_its_timeout__failed_and_its_slot_freed() -> None:
    """The execution is bounded by the action's timeout, so the slot's lease (timeout plus a
    margin) always outlives the call: no other write can take it over while this one runs."""
    clock = Clock()
    stuck = Scripted(clock, rest={"listing_id": 1}, hold=anyio.Event())
    shared = guards(clock)
    store = MemoryActionStore()
    service = gateway(stuck, clock, shared, store=store, timeout=timedelta(milliseconds=50))

    timed_out = await service.submit(write("inventory:1"))
    stuck.hold = None
    after = await service.submit(write("inventory:2"))

    assert (timed_out.outcome, timed_out.error_code) == (ActionOutcome.FAILED, "TimeoutError")
    assert after.outcome is ActionOutcome.SUCCEEDED


async def test_an_unexpected_local_error__failed_once_and_not_a_breaker_failure() -> None:
    clock = Clock()
    channel = Scripted(clock, script=[KeyError("bug")] * 6)
    service = gateway(channel, clock, guards(clock))

    for n in range(6):
        result = await service.submit(ask(key=f"receipts:{n}"))
        assert (result.outcome, result.attempts) == (ActionOutcome.FAILED, 1)
    healthy = await service.submit(ask(key="receipts:ok"))

    assert clock.slept == []
    assert healthy.outcome is ActionOutcome.SUCCEEDED  # a bug does not open the breaker


@pytest.mark.parametrize(
    "error",
    [
        MarketplaceUnavailableError("no answer"),
        ConnectionError("reset"),
        TimeoutError(),
        MarketplaceUnavailableError("bad gateway", status=502),
    ],
)
async def test_transport_failures_and_5xx__retried_for_reads(error: Exception) -> None:
    clock = Clock()
    channel = Scripted(clock, script=[error])
    service = gateway(channel, clock, guards(clock))

    result = await service.submit(ask())

    assert (result.outcome, result.attempts) == (ActionOutcome.SUCCEEDED, 2)


def test_gateway__refuses_guards_without_a_budget_for_an_action() -> None:
    clock = Clock()
    registry = office()
    catalog = replace(real_catalog(registry, built=True), rate_budgets={})
    world = World()

    with pytest.raises(ValueError, match="etsy:app"):
        ActionGatewayService(
            GatewayParts(
                catalog=catalog,
                registry=lambda: registry,
                store=MemoryActionStore(),
                principals=Principals(),
                shops=Shops(),
                approvals=Approvals(),
                adapters={Channel.API: world.channel},
                capabilities=Capabilities(),
                clock=clock,
                guards=guards(clock),
            )
        )
