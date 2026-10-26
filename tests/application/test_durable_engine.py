"""The durable automation engine (slice 5.10) with the in-memory store: replay, refusals, retry
policy, dead letters, admin retry, crash recovery and the state-change record. The same cases
run against Postgres in tests/integration/test_durable_engine.py."""

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, override

import pytest

from htco_application.automation.durable import (
    EXECUTION_JOB,
    LEASE_EXPIRED,
    DurableAutomationEngine,
    ExecutionNotFoundError,
    HandlerContext,
    HandlerOutcome,
    NotDeadLetteredError,
    NotWaitingError,
    StoredExecution,
)
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.execution.gateway import UnavailableGateway
from htco_application.kernel import JobError
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AUTOMATION_SPEC_SECTIONS, ExecutionState, TriggerKind
from tests.application.workforce_fixtures import (
    advisor_document,
    employee_document,
    tool_document,
)
from tests.automation_fakes import EffectBook, MemoryExecutionStore, engine_document

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
ORG = uuid.UUID("3a000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("3b000000-0000-0000-0000-000000000000")
ETSY = ActorRef(ActorKind.SYSTEM, "etsy-webhook")
WORKER = ActorRef(ActorKind.SYSTEM, "agent-worker:w1")
ADMIN = ActorRef(ActorKind.USER, "3a100000-0000-0000-0000-000000000001")


def registry(**changes: Any) -> Registry:
    return Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[engine_document(**changes)],
        advisors=[advisor_document()],
    )


class World:
    def __init__(self, *, handlers: bool = True, output_check: Any = None, **changes: Any) -> None:
        self.store = MemoryExecutionStore()
        self.book = EffectBook()
        self.now = NOW
        self.registry = registry(**changes)
        self.engine = DurableAutomationEngine(
            lambda: self.registry,
            self.store,
            {"etsy-order-intake": self.book.handle} if handlers else {},
            clock=lambda: self.now,
            jitter=lambda: 1.0,
            gateway=UnavailableGateway(),
            output_check=output_check,
        )


def paid(**changes: Any) -> TriggerRequest:
    values: dict[str, Any] = {
        "automation_id": "etsy-order-intake",
        "trigger": TriggerKind.EVENT,
        "actor": ETSY,
        "idempotency_key": "etsy:shop1:receipt3412987711:paid",
        "payload": {"receipt_id": "3412987711"},
        "correlation_id": "corr-1",
        "organization_id": ORG,
    } | changes
    return TriggerRequest(**values)


@pytest.mark.ac("AC-5.10.1")
async def test_trigger__replayed__one_execution_one_job_and_the_handler_runs_once() -> None:
    world = World()

    first = world.engine.trigger(paid())
    second = world.engine.trigger(paid())
    done = await world.engine.execute(first.id, worker=WORKER)
    replayed_job = await world.engine.execute(first.id, worker=WORKER)

    assert first.id == second.id
    assert first.state is ExecutionState.PENDING
    assert [job.kind for job in world.store.jobs] == [EXECUTION_JOB]
    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 1)
    assert replayed_job.state is ExecutionState.SUCCEEDED
    assert world.book.calls == 1


@pytest.mark.ac("AC-5.10.1")
def test_trigger__concurrently__one_execution() -> None:
    world = World()
    barrier = threading.Barrier(8)
    found: list[uuid.UUID] = []

    def trigger() -> None:
        barrier.wait(timeout=10)
        found.append(world.engine.trigger(paid()).id)

    threads = [threading.Thread(target=trigger) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(set(found)) == 1
    assert len(world.store.rows) == 1


@pytest.mark.ac("AC-5.10.2")
@pytest.mark.parametrize(
    ("world_changes", "request_changes", "reason"),
    [
        (
            {
                "readiness": "draft",
                "specification": {
                    name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS
                },
            },
            {},
            "not active",
        ),
        ({"handlers": False}, {}, "no handler deployed"),
        ({}, {"trigger": TriggerKind.AGENT_TOOL}, "cannot be triggered by agent_tool"),
        ({}, {"organization_id": None}, "belongs to an organization"),
        ({}, {"automation_id": "no-such-flow"}, "unknown automation"),
    ],
)
def test_trigger__refused__nothing_written_and_the_reason_logged(
    caplog: pytest.LogCaptureFixture,
    world_changes: dict[str, Any],
    request_changes: dict[str, Any],
    reason: str,
) -> None:
    world = World(**world_changes)

    with caplog.at_level(logging.WARNING), pytest.raises(AutomationRefused, match=reason):
        world.engine.trigger(paid(**request_changes))

    assert world.store.rows == {}
    assert world.store.jobs == []
    [record] = [r for r in caplog.records if r.getMessage() == "automation.refused"]
    assert reason in record.__dict__["reason"]


def test_trigger__payload_that_is_not_json__is_refused() -> None:
    world = World()

    with pytest.raises(AutomationRefused, match="not JSON"):
        world.engine.trigger(paid(payload={"when": NOW}))


async def test_a_deferred_read__the_retry_waits_for_the_gateways_retry_at() -> None:
    """Codex review of #108: a gateway deferral (for example until the daily quota resets)
    raises a rate limit with its wait; the retry runs then, not at the engine's own backoff,
    so the attempts are not spent before the gateway allows another call."""
    from htco_application.marketplace import MarketplaceRateLimitedError  # noqa: PLC0415

    world = World()

    async def deferred(context: HandlerContext) -> HandlerOutcome:  # noqa: ARG001
        raise MarketplaceRateLimitedError("etsy.reviews.read deferred", retry_after_seconds=3600)

    world.engine = DurableAutomationEngine(
        lambda: world.registry, world.store, {"etsy-order-intake": deferred},
        clock=lambda: world.now, jitter=lambda: 1.0, gateway=UnavailableGateway(),
    )  # fmt: skip
    execution = world.engine.trigger(paid())

    first = await world.engine.execute(execution.id, worker=WORKER)

    assert first.state is ExecutionState.RETRY_SCHEDULED
    assert world.store.jobs[1].run_at == NOW + timedelta(seconds=3600)


async def test_a_shorter_retry_after_never_shortens_the_backoff() -> None:
    from htco_application.marketplace import MarketplaceRateLimitedError  # noqa: PLC0415

    world = World()

    async def soon(context: HandlerContext) -> HandlerOutcome:  # noqa: ARG001
        raise MarketplaceRateLimitedError("slow down", retry_after_seconds=1)

    world.engine = DurableAutomationEngine(
        lambda: world.registry, world.store, {"etsy-order-intake": soon},
        clock=lambda: world.now, jitter=lambda: 1.0, gateway=UnavailableGateway(),
    )  # fmt: skip
    execution = world.engine.trigger(paid())

    await world.engine.execute(execution.id, worker=WORKER)

    assert world.store.jobs[1].run_at == NOW + timedelta(seconds=10)  # the policy's backoff


@pytest.mark.ac("AC-5.10.4")
async def test_failing_handler__retries_within_the_backoff_bounds_then_dead_letters() -> None:
    world = World()
    world.book.failures = 99
    execution = world.engine.trigger(paid())

    first = await world.engine.execute(execution.id, worker=WORKER)
    world.now += timedelta(seconds=10)
    last = await world.engine.execute(execution.id, worker=WORKER)

    assert (first.state, first.attempts, first.last_error) == (
        ExecutionState.RETRY_SCHEDULED,
        1,
        "ConnectionError",
    )
    retry_job = world.store.jobs[1]
    # Two attempts, 10 s initial backoff, jitter 1.0: the full 10 s (bounds: 5 s to 10 s).
    assert retry_job.run_at == NOW + timedelta(seconds=10)
    assert retry_job.idempotency_key == f"execution:{execution.id}:2"
    assert (last.state, last.attempts, last.last_error) == (
        ExecutionState.DEAD_LETTERED,
        2,
        "ConnectionError",
    )
    assert len(world.store.jobs) == 2  # no job after the dead letter
    assert world.book.calls == 2


@pytest.mark.ac("AC-5.10.4")
async def test_admin_retry__reuses_the_dead_lettered_execution_and_is_audited() -> None:
    world = World()
    world.book.failures = 2
    execution = world.engine.trigger(paid())
    await world.engine.execute(execution.id, worker=WORKER)
    await world.engine.execute(execution.id, worker=WORKER)

    retried = world.engine.retry_dead_lettered(
        ORG, execution.id, actor=ADMIN, correlation_id="req-9"
    )
    done = await world.engine.execute(execution.id, worker=WORKER)

    assert retried.id == execution.id
    assert retried.state is ExecutionState.RETRY_SCHEDULED
    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 3)
    [audit] = world.store.audits
    assert (audit.action, audit.actor, audit.entity_id, audit.correlation_id) == (
        "automation.execution_retried",
        ADMIN,
        str(execution.id),
        "req-9",
    )
    assert world.book.effects == {"3412987711": 1}


def test_admin_retry__not_dead_lettered_or_another_organization__is_refused() -> None:
    world = World()
    execution = world.engine.trigger(paid())

    with pytest.raises(NotDeadLetteredError):
        world.engine.retry_dead_lettered(ORG, execution.id, actor=ADMIN, correlation_id="c")
    with pytest.raises(ExecutionNotFoundError):
        world.engine.retry_dead_lettered(OTHER_ORG, execution.id, actor=ADMIN, correlation_id="c")
    with pytest.raises(ExecutionNotFoundError):
        world.engine.retry_dead_lettered(ORG, uuid.uuid7(), actor=ADMIN, correlation_id="c")


@pytest.mark.ac("AC-5.10.5")
async def test_worker_died_while_running__the_next_run_succeeds_once() -> None:
    world = World()
    execution = world.engine.trigger(paid())
    # A worker moved it to RUNNING and died before finishing (its job's lease expired).
    running = execution.transition(ExecutionState.RUNNING)
    world.store.rows[execution.id] = StoredExecution(
        running, world.store.rows[execution.id].payload
    )

    done = await world.engine.execute(execution.id, worker=WORKER)

    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 2)
    assert world.book.effects == {"3412987711": 1}
    reasons = [change.reason for change in world.store.changes]
    assert LEASE_EXPIRED in reasons


async def test_worker_died_on_the_last_attempt__dead_letters_without_running() -> None:
    world = World()
    execution = world.engine.trigger(paid())
    second = (
        execution.transition(ExecutionState.RUNNING)
        .transition(ExecutionState.FAILED)
        .transition(ExecutionState.RETRY_SCHEDULED)
        .transition(ExecutionState.RUNNING)
    )
    world.store.rows[execution.id] = StoredExecution(second, world.store.rows[execution.id].payload)

    done = await world.engine.execute(execution.id, worker=WORKER)

    assert done.state is ExecutionState.DEAD_LETTERED
    assert done.last_error == LEASE_EXPIRED
    assert world.book.calls == 0


@pytest.mark.ac("AC-5.10.6")
async def test_every_state_change__is_recorded_with_its_actor_and_correlation() -> None:
    world = World()
    world.book.failures = 1
    execution = world.engine.trigger(paid())
    await world.engine.execute(execution.id, worker=WORKER)
    await world.engine.execute(execution.id, worker=WORKER)

    path = [(c.from_state, c.to_state, c.actor, c.correlation_id) for c in world.store.changes]
    assert path == [
        (None, ExecutionState.PENDING, ETSY, "corr-1"),
        (ExecutionState.PENDING, ExecutionState.RUNNING, WORKER, "corr-1"),
        (ExecutionState.RUNNING, ExecutionState.FAILED, WORKER, "corr-1"),
        (ExecutionState.FAILED, ExecutionState.RETRY_SCHEDULED, WORKER, "corr-1"),
        (ExecutionState.RETRY_SCHEDULED, ExecutionState.RUNNING, WORKER, "corr-1"),
        (ExecutionState.RUNNING, ExecutionState.SUCCEEDED, WORKER, "corr-1"),
    ]
    assert {c.organization_id for c in world.store.changes} == {ORG}


async def test_execute__handler_removed_since_the_trigger__fails_into_the_retry_policy() -> None:
    world = World()
    execution = world.engine.trigger(paid())
    world.engine = DurableAutomationEngine(
        lambda: world.registry,
        world.store,
        {},
        clock=lambda: world.now,
        jitter=lambda: 0.0,
        gateway=UnavailableGateway(),
    )

    result = await world.engine.execute(execution.id, worker=WORKER)

    assert result.state is ExecutionState.RETRY_SCHEDULED
    assert result.last_error == "JobError: etsy-order-intake has no handler deployed"


async def test_execute__unknown_execution_or_definition__is_a_job_error() -> None:
    world = World()
    execution = world.engine.trigger(paid())
    with pytest.raises(JobError, match="no execution"):
        await world.engine.execute(uuid.uuid7(), worker=WORKER)

    world.registry = Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[engine_document("other-flow")],
        advisors=[advisor_document()],
    )
    with pytest.raises(JobError, match="no definition"):
        await world.engine.execute(execution.id, worker=WORKER)


async def test_execute__another_run_moved_it_first__changes_nothing() -> None:
    world = World()
    execution = world.engine.trigger(paid())

    store = world.store
    original = store.advance
    calls: list[int] = []

    def advance_once_lost(*args: Any, **kwargs: Any) -> bool:
        calls.append(1)
        if len(calls) == 1:
            return False  # another worker claimed the run between load and update
        return original(*args, **kwargs)

    store.advance = advance_once_lost  # type: ignore[method-assign]

    result = await world.engine.execute(execution.id, worker=WORKER)

    assert result.state is ExecutionState.PENDING
    assert world.book.calls == 0


@pytest.mark.ac("AC-5.10.6")
async def test_handler__gets_its_context_with_the_gateway() -> None:
    """ADR-0019: the handler knows its run and acts through the Action Gateway."""
    world = World()
    execution = world.engine.trigger(paid(causation_id="evt-7"))

    await world.engine.execute(execution.id, worker=WORKER)

    [context] = world.book.contexts
    assert (context.execution_id, context.automation_id, context.attempt) == (
        execution.id,
        "etsy-order-intake",
        1,
    )
    assert (context.organization_id, context.actor, context.correlation_id) == (ORG, ETSY, "corr-1")
    assert context.causation_id == "evt-7"
    assert isinstance(context.gateway, UnavailableGateway)


async def test_waiting_action__the_run_waits_without_a_job_then_resumes_once() -> None:
    """ADR-0019: RUNNING -> WAITING, the job ends; resume -> RETRY_SCHEDULED with a new job."""
    world = World()
    world.book.wait_for_approval = True
    execution = world.engine.trigger(paid())

    waiting = await world.engine.execute(execution.id, worker=WORKER)
    replayed = await world.engine.execute(execution.id, worker=WORKER)
    resumed = world.engine.resume(ORG, execution.id, actor=ADMIN, reason="approval_decided")
    done = await world.engine.execute(execution.id, worker=WORKER)

    assert waiting.state is ExecutionState.WAITING
    assert replayed.state is ExecutionState.WAITING
    assert len(world.store.jobs) == 2  # the trigger's and the resume's
    assert resumed.state is ExecutionState.RETRY_SCHEDULED
    assert (done.state, done.attempts) == (ExecutionState.SUCCEEDED, 1)
    assert world.book.calls == 2
    reasons = [(c.to_state, c.reason) for c in world.store.changes]
    assert (ExecutionState.WAITING, "pending_approval") in reasons
    assert (ExecutionState.RETRY_SCHEDULED, "approval_decided") in reasons


async def test_waiting_action__rejected__the_execution_is_cancelled() -> None:
    world = World()
    world.book.wait_for_approval = True
    execution = world.engine.trigger(paid())
    await world.engine.execute(execution.id, worker=WORKER)

    cancelled = world.engine.cancel_waiting(ORG, execution.id, actor=ADMIN, reason="rejected")

    assert cancelled.state is ExecutionState.CANCELLED
    assert cancelled.is_terminal


def test_resume__an_execution_that_is_not_waiting__is_refused() -> None:
    world = World()
    execution = world.engine.trigger(paid())

    with pytest.raises(NotWaitingError):
        world.engine.resume(ORG, execution.id, actor=ADMIN, reason="x")
    with pytest.raises(ExecutionNotFoundError):
        world.engine.cancel_waiting(OTHER_ORG, execution.id, actor=ADMIN, reason="x")


async def test_unavailable_gateway__refuses_every_action() -> None:
    from htco_application.execution.gateway import (  # noqa: PLC0415
        ActionOutcome,
        ActionRef,
        ActionRequest,
    )

    result = await UnavailableGateway().submit(
        ActionRequest(
            action=ActionRef("etsy.receipts.read", 1),
            actor=ETSY,
            organization_id=ORG,
            idempotency_key="k",
            correlation_id="c",
        )
    )

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "gateway_unavailable")
    assert not result.outcome.waits
    assert ActionOutcome.PENDING_APPROVAL.waits


async def test_execution_jobs__share_one_lock_per_execution__so_two_runs_never_overlap() -> None:
    """Post-merge review: a job replayed after its worker died (after recording the failure but
    before completing the job) and the retry job that run had enqueued could both be claimed; the
    second found the execution RUNNING, took it for a dead run and ran the handler again while the
    first was still running it. Every job of an execution holds the same lock key, so the queue
    never runs two of them at once (AC-5.1.2)."""
    world = World()
    world.book.failures = 1
    execution = world.engine.trigger(paid())

    await world.engine.execute(execution.id, worker=WORKER)  # fails: a retry job
    world.book.wait_for_approval = True
    await world.engine.execute(execution.id, worker=WORKER)  # waits
    world.engine.resume(ORG, execution.id, actor=ADMIN, reason="approval_decided")

    assert len(world.store.jobs) == 3  # the trigger's, the retry's and the resume's
    assert {job.lock_key for job in world.store.jobs} == {f"automation_execution:{execution.id}"}


@pytest.mark.ac("AC-5.0.2")
async def test_execution_jobs__carry_the_triggers_correlation_and_the_execution_as_cause() -> None:
    world = World()

    execution = world.engine.trigger(paid(correlation_id="corr-trace"))

    [job] = world.store.jobs
    assert (job.correlation_id, job.causation_id) == ("corr-trace", str(execution.id))


# --- #127: a completed execution keeps its handler's output, once checked ------------------


async def test_completed__the_checked_output__kept_with_the_execution() -> None:
    checked: list[tuple[str, dict[str, Any]]] = []

    def check(automation_id: str, output: Any) -> dict[str, Any]:
        checked.append((automation_id, dict(output)))
        return dict(output)

    world = World(output_check=check)
    execution = world.engine.trigger(paid())

    done = await world.engine.execute(execution.id, worker=WORKER)

    stored = world.store.load(execution.id)
    assert stored is not None
    assert done.output == {"order": "3412987711"}
    assert stored.execution.output == {"order": "3412987711"}
    assert checked == [("etsy-order-intake", {"order": "3412987711"})]


async def test_completed__an_output_the_check_refuses__not_kept() -> None:
    world = World(output_check=lambda automation_id, output: None)  # noqa: ARG005
    execution = world.engine.trigger(paid())

    done = await world.engine.execute(execution.id, worker=WORKER)

    assert done.state is ExecutionState.SUCCEEDED
    assert done.output is None


async def test_completed__without_an_output_check__nothing_kept() -> None:
    world = World()
    execution = world.engine.trigger(paid())

    done = await world.engine.execute(execution.id, worker=WORKER)

    assert done.output is None


async def test_completed__a_check_that_raises__the_run_still_succeeds_without_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing after the handler's work may fail the run (quill on #158)."""

    def check(automation_id: str, output: Any) -> dict[str, Any]:  # noqa: ARG001
        raise RuntimeError("a bug in the check")

    world = World(output_check=check)
    execution = world.engine.trigger(paid())

    with caplog.at_level(logging.WARNING):
        done = await world.engine.execute(execution.id, worker=WORKER)

    assert (done.state, done.output) == (ExecutionState.SUCCEEDED, None)
    assert any(r.getMessage() == "automation.output_check_failed" for r in caplog.records)


class OutputRefusingStore(MemoryExecutionStore):
    """A store whose database refuses the output (for example its size check)."""

    @override
    def advance(self, before: Any, steps: Any, **kwargs: Any) -> bool:
        after, _ = steps[-1]
        if after.output is not None:
            raise ValueError("automation_executions_output_check")
        return super().advance(before, steps, **kwargs)


async def test_completed__an_output_the_store_refuses__the_run_still_succeeds_without_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World(output_check=lambda automation_id, output: dict(output))  # noqa: ARG005
    world.store = OutputRefusingStore()
    world.engine._store = world.store
    execution = world.engine.trigger(paid())

    with caplog.at_level(logging.WARNING):
        done = await world.engine.execute(execution.id, worker=WORKER)

    stored = world.store.load(execution.id)
    assert stored is not None
    assert (done.state, done.output) == (ExecutionState.SUCCEEDED, None)
    assert stored.execution.state is ExecutionState.SUCCEEDED
    assert any(r.getMessage() == "automation.output_not_saved" for r in caplog.records)
