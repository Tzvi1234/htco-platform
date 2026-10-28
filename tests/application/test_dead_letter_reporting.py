"""AC-6.7.4: a dead-lettered execution is reported through the engine's `on_dead_letter`
callback, once, with identifiers and the stored error only (never buyer data), and a failing
reporter never changes the dead letter."""

import logging
from datetime import timedelta
from typing import Any

import pytest

from htco_application.automation.durable import (
    LEASE_EXPIRED,
    DeadLetter,
    DurableAutomationEngine,
    Handler,
    HandlerContext,
    HandlerOutcome,
    StoredExecution,
)
from htco_application.execution.gateway import UnavailableGateway
from htco_domain.automation import ExecutionState
from tests.application.test_durable_engine import NOW, WORKER, paid, registry
from tests.automation_fakes import EffectBook, MemoryExecutionStore

pytestmark = pytest.mark.anyio

BUYER_EMAIL = "jane.buyer@example.com"
BUYER_NAME = "Jane Buyer"
BUYER_ADDRESS = "12 Elm Street, Springfield"


class Reports:
    def __init__(self, *, fails: bool = False) -> None:
        self.received: list[DeadLetter] = []
        self.fails = fails

    def __call__(self, dead: DeadLetter) -> None:
        self.received.append(dead)
        if self.fails:
            raise RuntimeError("sentry is down")


class World:
    def __init__(self, reports: Reports, *, handler: Handler | None = None) -> None:
        self.store = MemoryExecutionStore()
        self.book = EffectBook()
        self.now = NOW
        self.registry = registry()
        self.engine = DurableAutomationEngine(
            lambda: self.registry,
            self.store,
            {"etsy-order-intake": handler or self.book.handle},
            clock=lambda: self.now,
            jitter=lambda: 1.0,
            gateway=UnavailableGateway(),
            on_dead_letter=reports,
        )

    async def run_out_of_retries(self) -> ExecutionState:
        execution = self.engine.trigger(
            paid(payload={"receipt_id": "3412987711", "buyer_email": BUYER_EMAIL})
        )
        await self.engine.execute(execution.id, worker=WORKER)
        self.now += timedelta(seconds=10)
        return (await self.engine.execute(execution.id, worker=WORKER)).state


@pytest.mark.ac("AC-6.7.4")
async def test_dead_letter__reported_once_with_the_safe_fields() -> None:
    reports = Reports()
    world = World(reports)
    world.book.failures = 99

    state = await world.run_out_of_retries()

    assert state is ExecutionState.DEAD_LETTERED
    [execution] = world.store.rows.values()
    assert reports.received == [
        DeadLetter(
            automation_id="etsy-order-intake",
            execution_id=execution.execution.id,
            error_type="ConnectionError",
            error="ConnectionError",
        )
    ]


@pytest.mark.ac("AC-6.7.4")
async def test_a_retry_that_succeeds__nothing_reported() -> None:
    reports = Reports()
    world = World(reports)
    world.book.failures = 1

    state = await world.run_out_of_retries()

    assert state is ExecutionState.SUCCEEDED
    assert reports.received == []


@pytest.mark.ac("AC-6.7.4")
async def test_a_reporter_that_raises__the_dead_letter_still_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reports = Reports(fails=True)
    world = World(reports)
    world.book.failures = 99

    with caplog.at_level(logging.WARNING):
        state = await world.run_out_of_retries()

    assert state is ExecutionState.DEAD_LETTERED
    [stored] = world.store.rows.values()
    assert stored.execution.state is ExecutionState.DEAD_LETTERED
    assert len(reports.received) == 1
    [record] = [
        r for r in caplog.records if r.getMessage() == "automation.dead_letter_report_failed"
    ]
    assert record.__dict__["error"] == "RuntimeError"


@pytest.mark.ac("AC-6.7.4")
async def test_dead_letter_report__carries_no_buyer_data() -> None:
    async def leaky(context: HandlerContext) -> HandlerOutcome:  # noqa: ARG001
        raise ValueError(f"no shipment for {BUYER_NAME} <{BUYER_EMAIL}> at {BUYER_ADDRESS}")

    reports = Reports()
    world = World(reports, handler=leaky)

    await world.run_out_of_retries()

    [dead] = reports.received
    assert (dead.error_type, dead.error) == ("ValueError", "ValueError")
    for secret in (BUYER_EMAIL, BUYER_NAME, BUYER_ADDRESS, "3412987711"):
        assert secret not in repr(dead)


@pytest.mark.ac("AC-6.7.4")
async def test_last_attempt__another_run_moved_it_first__nothing_reported() -> None:
    reports = Reports()
    world = World(reports)
    world.book.failures = 99
    execution = world.engine.trigger(paid())
    await world.engine.execute(execution.id, worker=WORKER)
    world.now += timedelta(seconds=10)
    original = world.store.advance

    def lose_the_dead_letter(before: Any, steps: Any, **kwargs: Any) -> bool:
        if steps[-1][0].state is ExecutionState.DEAD_LETTERED:
            return False  # another run changed the execution first: nothing is written
        return original(before, steps, **kwargs)

    world.store.advance = lose_the_dead_letter  # type: ignore[method-assign]

    await world.engine.execute(execution.id, worker=WORKER)

    assert world.store.rows[execution.id].execution.state is not ExecutionState.DEAD_LETTERED
    assert reports.received == []


@pytest.mark.ac("AC-6.7.4")
async def test_worker_died_on_the_last_attempt__the_dead_letter_is_reported() -> None:
    reports = Reports()
    world = World(reports)
    execution = world.engine.trigger(paid())
    second = (
        execution.transition(ExecutionState.RUNNING)
        .transition(ExecutionState.FAILED)
        .transition(ExecutionState.RETRY_SCHEDULED)
        .transition(ExecutionState.RUNNING)
    )
    world.store.rows[execution.id] = StoredExecution(second, world.store.rows[execution.id].payload)

    await world.engine.execute(execution.id, worker=WORKER)

    [dead] = reports.received
    assert (dead.execution_id, dead.error_type, dead.error) == (
        execution.id,
        "LeaseExpired",
        LEASE_EXPIRED,
    )
