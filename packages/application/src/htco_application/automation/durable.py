"""The durable automation engine (slice 5.10, ADR-0013): executions on the job queue.

`trigger` is the one entry point for every trigger kind. It authorizes the request (the same gate
as the in-memory engine: readiness, deployed handler, trigger kind, employee grants) and logs a
refusal with its reason before anything is written (AC-5.10.2). Then it creates the execution,
its first state change and its first job (`automation.execute`) in one transaction; the store's
unique `(automation_id, idempotency_key)` makes a replay, even a concurrent one, return the first
execution (AC-5.10.1).

`execute` runs inside the worker's job. It moves the execution to `RUNNING`, calls the handler,
and records `SUCCEEDED`, or `FAILED` then `RETRY_SCHEDULED` with the next job at the definition's
backoff with jitter (never sooner than an error's `retry_after_seconds`, such as the gateway's
deferral until a quota resets), or `DEAD_LETTERED` with the last error (AC-5.10.4). An execution
found `RUNNING` belongs to a worker that died (its job's lease expired): that run counts as a failed
attempt and the execution goes on from there, so it succeeds once (AC-5.10.5). Every state change
is stored with its actor and correlation ID, and the domain refuses any transition outside the
`ExecutionState` machine (AC-5.10.6).

Handlers (ADR-0019) receive a `HandlerContext`: the execution's identity, tenant, actor,
correlation and causation IDs, its payload, and an `ActionGateway` client. Business effects go
through `gateway.submit(...)`, never straight to use cases or the outbox. A handler whose action
needs an approval or a person returns `Waiting`: the execution moves to `WAITING` and its job
finishes, so no lease is held for days; `resume` (an approval decided, a task done) sends it back
to `RETRY_SCHEDULED` with a new job, and `cancel_waiting` (rejected, expired) ends it `CANCELLED`.

The store is synchronous (psycopg's pool); `execute` is asynchronous, because handlers await the
gateway, and runs every store call through the injected `offload` (a worker thread in production).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.audit import AuditRecord
from htco_application.automation.engine import AutomationRefused, TriggerRequest, authorize
from htco_application.execution.gateway import ActionGateway
from htco_application.jobs import JobError, JobSpec, JsonValue, describe_error
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorRef
from htco_domain.automation import (
    AutomationDefinition,
    AutomationExecution,
    ExecutionState,
    RetryPolicy,
)

EXECUTION_JOB = "automation.execute"
# The job only carries an execution; crashes are retried by the queue, failures of the handler
# by the execution's own policy (a new job per attempt). A crash also uses an execution attempt,
# so the job outlives the execution's attempts and the execution, not the job, dead-letters.
EXECUTION_JOB_BACKOFF = (timedelta(seconds=5), timedelta(minutes=5))


def execution_job_retry(definition: AutomationDefinition) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max(5, definition.retry.max_attempts + 1),
        initial_backoff=EXECUTION_JOB_BACKOFF[0],
        max_backoff=EXECUTION_JOB_BACKOFF[1],
    )


LEASE_EXPIRED = "LeaseExpired: the worker stopped during the run"


@dataclass(frozen=True)
class HandlerContext:
    """What a handler knows about its run (ADR-0019)."""

    execution_id: uuid.UUID
    automation_id: str
    actor: ActorRef  # who triggered the execution
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    correlation_id: str
    causation_id: str | None
    attempt: int
    payload: Mapping[str, JsonValue]
    gateway: ActionGateway


@dataclass(frozen=True)
class Completed:
    output: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class Waiting:
    """An action of the handler waits for an approval or a person."""

    reason: str  # "pending_approval" or "handed_to_human"
    action_execution_id: uuid.UUID | None = None


type HandlerOutcome = Completed | Waiting
type Handler = Callable[[HandlerContext], Awaitable[HandlerOutcome]]


class Offload(Protocol):
    """Runs a blocking call off the event loop (`anyio.to_thread.run_sync` in production)."""

    def __call__[T](self, work: Callable[[], T], /) -> Awaitable[T]: ...


# What a completed run's output may be kept as (#127): the output to store, or None to keep
# nothing. `TypedOutputs.check` in production.
type OutputCheck = Callable[[str, Mapping[str, JsonValue]], Mapping[str, JsonValue] | None]


async def run_inline[T](work: Callable[[], T]) -> T:
    """Runs store calls in the event loop (tests and in-memory stores)."""
    return work()


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateChange:
    """One recorded transition of an execution (AC-5.10.6)."""

    execution_id: uuid.UUID
    organization_id: uuid.UUID
    from_state: ExecutionState | None
    to_state: ExecutionState
    actor: ActorRef
    correlation_id: str
    at: datetime
    reason: str | None = None


@dataclass(frozen=True)
class StoredExecution:
    execution: AutomationExecution
    payload: Mapping[str, JsonValue]


class DurableExecutionStore(Protocol):
    def create(
        self,
        execution: AutomationExecution,
        payload: Mapping[str, JsonValue],
        *,
        change: StateChange,
        job: JobSpec,
    ) -> AutomationExecution:
        """In one transaction: insert the execution, its first state change and its job, unless
        `(automation_id, idempotency_key)` exists; return the stored execution either way."""
        ...

    def load(self, execution_id: uuid.UUID) -> StoredExecution | None: ...

    def advance(
        self,
        before: AutomationExecution,
        steps: Sequence[tuple[AutomationExecution, StateChange]],
        *,
        job: JobSpec | None = None,
        audit: AuditRecord | None = None,
    ) -> bool:
        """In one transaction, only while the execution is still as `before` (its state, attempts
        and the action it waits for, `waiting_for`): store the
        last step's execution, every step's state change, the job and the audit record. False
        when another run changed it first (nothing is written)."""
        ...


class ExecutionNotFoundError(Exception):
    pass


class NotDeadLetteredError(Exception):
    pass


class NotWaitingError(Exception):
    pass


def execution_job(
    execution: AutomationExecution,
    definition: AutomationDefinition,
    *,
    attempt: int | str,
    run_at: datetime | None,
) -> JobSpec:
    return JobSpec(
        kind=EXECUTION_JOB,
        idempotency_key=f"execution:{execution.id}:{attempt}",
        payload={"execution_id": str(execution.id)},
        organization_id=execution.organization_id,
        shop_id=execution.shop_id,
        run_at=run_at,
        retry=execution_job_retry(definition),
        # One run of an execution at a time: a job replayed after a crash and the retry job its
        # run had enqueued never run the handler side by side (post-merge review of 2026-10-23).
        lock_key=f"automation_execution:{execution.id}",
        # The trigger's correlation ID, and the execution as the job's cause (AC-5.0.2).
        correlation_id=execution.correlation_id,
        causation_id=str(execution.id),
    )


class DurableAutomationEngine:
    def __init__(  # noqa: PLR0913 - the engine's collaborators, all injected
        self,
        registry: Callable[[], Registry],
        store: DurableExecutionStore,
        handlers: Mapping[str, Handler],
        *,
        clock: Callable[[], datetime],
        jitter: Callable[[], float],
        gateway: ActionGateway,
        new_id: Callable[[], uuid.UUID] = uuid.uuid7,
        offload: Offload = run_inline,
        output_check: OutputCheck | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._handlers = handlers
        self._clock = clock
        self._jitter = jitter
        self._gateway = gateway
        self._new_id = new_id
        self._offload = offload
        self._output_check = output_check

    def definition(self, automation_id: str) -> AutomationDefinition | None:
        return self._registry().automations.get(automation_id)

    # --- Triggers -------------------------------------------------------------------------

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        try:
            definition = self._authorize(request)
        except AutomationRefused as refused:
            _log.warning(
                "automation.refused",
                extra={
                    "automation_id": request.automation_id,
                    "trigger": request.trigger.value,
                    "actor": str(request.actor),
                    "reason": str(refused),
                    "correlation_id": request.correlation_id,
                },
            )
            raise
        now = self._clock()
        execution = AutomationExecution(
            id=self._new_id(),
            automation_id=definition.id,
            automation_version=definition.version,
            idempotency_key=request.idempotency_key,
            trigger=request.trigger,
            actor=request.actor,
            state=ExecutionState.PENDING,
            attempts=0,
            correlation_id=request.correlation_id,
            organization_id=request.organization_id,
            shop_id=request.shop_id,
            causation_id=request.causation_id,
        )
        return self._store.create(
            execution,
            _json(request.payload),
            change=self._change(execution, None, ExecutionState.PENDING, request.actor, now),
            job=execution_job(execution, definition, attempt=1, run_at=now),
        )

    def _authorize(self, request: TriggerRequest) -> AutomationDefinition:
        definition = authorize(self._registry(), self._handlers, request)
        if request.organization_id is None:
            raise AutomationRefused("a durable execution belongs to an organization")
        return definition

    def retry_dead_lettered(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        actor: ActorRef,
        correlation_id: str,
        automation_id: str | None = None,
    ) -> AutomationExecution:
        """An admin's retry (AC-5.10.4): the same execution goes back to `RETRY_SCHEDULED` with a
        new job, audited. Raises `ExecutionNotFoundError` or `NotDeadLetteredError`."""
        stored = self._store.load(execution_id)
        if (
            stored is None
            or stored.execution.organization_id != organization_id
            or (automation_id is not None and stored.execution.automation_id != automation_id)
        ):
            raise ExecutionNotFoundError(f"no execution {execution_id}")
        execution = stored.execution
        if execution.state is not ExecutionState.DEAD_LETTERED:
            raise NotDeadLetteredError(f"{execution_id} is {execution.state.value}")
        definition = self._registry().automations.get(execution.automation_id)
        if definition is None:
            raise ExecutionNotFoundError(f"no definition for {execution.automation_id}")
        now = self._clock()
        retried = execution.transition(ExecutionState.RETRY_SCHEDULED)
        change = replace(
            self._change(execution, execution.state, retried.state, actor, now),
            correlation_id=correlation_id,
            reason="admin_retry",
        )
        written = self._store.advance(
            execution,
            [(retried, change)],
            job=execution_job(execution, definition, attempt=f"retry:{self._new_id()}", run_at=now),
            audit=AuditRecord.new(
                organization_id=organization_id,
                actor=actor,
                action="automation.execution_retried",
                entity_type="automation_execution",
                entity_id=str(execution.id),
                before={"state": execution.state.value, "attempts": execution.attempts},
                after={"state": retried.state.value},
                correlation_id=correlation_id,
            ),
        )
        if not written:
            raise NotDeadLetteredError(f"{execution_id} changed meanwhile")
        return retried

    # --- The worker's job -------------------------------------------------------------------

    async def execute(self, execution_id: uuid.UUID, *, worker: ActorRef) -> AutomationExecution:
        stored = await self._offload(lambda: self._store.load(execution_id))
        if stored is None:
            raise JobError(f"no execution {execution_id}")
        execution = stored.execution
        if execution.is_terminal or execution.state is ExecutionState.WAITING:
            return execution  # a replayed job, or a run waiting for its resume: nothing runs
        definition = self._registry().automations.get(execution.automation_id)
        if definition is None:
            raise JobError(f"no definition for {execution.automation_id}")
        now = self._clock()
        if execution.state is ExecutionState.RUNNING:
            recovered = await self._offload(
                lambda: self._recover(definition, execution, worker, now)
            )
            if recovered is None or recovered.state is not ExecutionState.RETRY_SCHEDULED:
                return await self._reload(execution_id)
            execution = recovered
        before = execution
        running = before.transition(ExecutionState.RUNNING)
        change = self._change(before, before.state, running.state, worker, now)
        if not await self._offload(lambda: self._store.advance(before, [(running, change)])):
            return await self._reload(execution_id)
        return await self._run(definition, running, stored.payload, worker)

    async def _run(
        self,
        definition: AutomationDefinition,
        running: AutomationExecution,
        payload: Mapping[str, JsonValue],
        worker: ActorRef,
    ) -> AutomationExecution:
        try:
            outcome = await self._handler(definition)(self._context(running, payload))
        except Exception as error:
            reason = describe_error(error)
            wait = _retry_after(error)
            return await self._offload(
                lambda: self._fail(definition, running, reason, worker, retry_after=wait)
            )
        now = self._clock()
        if isinstance(outcome, Waiting):
            # The wait is bound to its action, so only that action's decision resumes it.
            waiting = replace(
                running.transition(ExecutionState.WAITING),
                waiting_for=outcome.action_execution_id,
            )
            change = self._change(
                running, running.state, waiting.state, worker, now, outcome.reason
            )
            await self._offload(lambda: self._store.advance(running, [(waiting, change)]))
            return waiting
        # The handler's work is done: from here nothing may fail the run (#158). A check or a
        # store that refuses the output costs the output, never the success.
        kept = self._kept_output(definition.id, outcome.output)
        done = replace(running.transition(ExecutionState.SUCCEEDED), output=kept)
        change = self._change(running, running.state, done.state, worker, now)
        try:
            await self._offload(lambda: self._store.advance(running, [(done, change)]))
        except Exception as error:
            if done.output is None:
                raise
            _log.warning(
                "automation.output_not_saved",
                extra={"automation_id": definition.id, "error": describe_error(error)},
            )
            done = replace(done, output=None)
            await self._offload(lambda: self._store.advance(running, [(done, change)]))
        return done

    def _kept_output(
        self, automation_id: str, output: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue] | None:
        if self._output_check is None:
            return None
        try:
            return self._output_check(automation_id, output)
        except Exception as error:  # a bug in the check must not fail a finished run
            _log.warning(
                "automation.output_check_failed",
                extra={"automation_id": automation_id, "error": describe_error(error)},
            )
            return None

    def _context(
        self, running: AutomationExecution, payload: Mapping[str, JsonValue]
    ) -> HandlerContext:
        if running.organization_id is None:  # pragma: no cover - refused at the trigger
            raise JobError("a durable execution belongs to an organization")
        return HandlerContext(
            execution_id=running.id,
            automation_id=running.automation_id,
            actor=running.actor,
            organization_id=running.organization_id,
            shop_id=running.shop_id,
            correlation_id=running.correlation_id,
            causation_id=running.causation_id,
            attempt=running.attempts,
            payload=payload,
            gateway=self._gateway,
        )

    # --- Waiting (an approval or a person) -------------------------------------------------

    def resume(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        actor: ActorRef,
        reason: str,
        waiting_for: uuid.UUID | None = None,
    ) -> AutomationExecution:
        """A waiting execution runs again (an approval was decided, a task was done): back to
        `RETRY_SCHEDULED` with a new job, without using an attempt of its retry policy.

        `waiting_for` names the action whose decision this is: an execution that now waits for
        another action (a replayed decision after a later wait) raises `NotWaitingError`."""
        execution, definition = self._waiting(organization_id, execution_id, waiting_for)
        now = self._clock()
        resumed = replace(execution.transition(ExecutionState.RETRY_SCHEDULED))
        # The waiting run did not fail: its attempt is given back.
        resumed = replace(resumed, attempts=max(0, execution.attempts - 1))
        change = self._change(execution, execution.state, resumed.state, actor, now, reason)
        job = execution_job(execution, definition, attempt=f"resume:{self._new_id()}", run_at=now)
        if not self._store.advance(execution, [(resumed, change)], job=job):
            raise NotWaitingError(f"{execution_id} changed meanwhile")
        return resumed

    def cancel_waiting(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        *,
        actor: ActorRef,
        reason: str,
        waiting_for: uuid.UUID | None = None,
    ) -> AutomationExecution:
        """A waiting execution ends (the approval was rejected or expired); `waiting_for` as in
        `resume`."""
        execution, _ = self._waiting(organization_id, execution_id, waiting_for)
        cancelled = execution.transition(ExecutionState.CANCELLED)
        change = self._change(
            execution, execution.state, cancelled.state, actor, self._clock(), reason
        )
        if not self._store.advance(execution, [(cancelled, change)]):
            raise NotWaitingError(f"{execution_id} changed meanwhile")
        return cancelled

    def _waiting(
        self,
        organization_id: uuid.UUID,
        execution_id: uuid.UUID,
        waiting_for: uuid.UUID | None,
    ) -> tuple[AutomationExecution, AutomationDefinition]:
        stored = self._store.load(execution_id)
        if stored is None or stored.execution.organization_id != organization_id:
            raise ExecutionNotFoundError(f"no execution {execution_id}")
        if stored.execution.state is not ExecutionState.WAITING:
            raise NotWaitingError(f"{execution_id} is {stored.execution.state.value}")
        if waiting_for is not None and stored.execution.waiting_for != waiting_for:
            # The store's `advance` checks the same wait, so a change in between is refused too.
            raise NotWaitingError(f"{execution_id} waits for another action now")
        definition = self._registry().automations.get(stored.execution.automation_id)
        if definition is None:
            raise ExecutionNotFoundError(f"no definition for {stored.execution.automation_id}")
        return stored.execution, definition

    def _handler(self, definition: AutomationDefinition) -> Handler:
        handler = self._handlers.get(definition.id)
        if handler is None:
            raise JobError(f"{definition.id} has no handler deployed")
        return handler

    def _fail(
        self,
        definition: AutomationDefinition,
        running: AutomationExecution,
        error: str,
        worker: ActorRef,
        *,
        retry_after: timedelta | None = None,
    ) -> AutomationExecution:
        now = self._clock()
        steps, job = self._failure_steps(
            definition, running, error, worker, now, retry_after=retry_after
        )
        self._store.advance(running, steps, job=job)
        return steps[-1][0]

    def _failure_steps(  # noqa: PLR0913 - the failure, and the wait it may ask for
        self,
        definition: AutomationDefinition,
        running: AutomationExecution,
        error: str,
        worker: ActorRef,
        now: datetime,
        *,
        retry_after: timedelta | None = None,
    ) -> tuple[list[tuple[AutomationExecution, StateChange]], JobSpec | None]:
        failed = running.transition(ExecutionState.FAILED, error=error)
        steps = [(failed, self._change(running, running.state, failed.state, worker, now, error))]
        if failed.attempts >= definition.retry.max_attempts:
            dead = failed.transition(ExecutionState.DEAD_LETTERED)
            steps.append((dead, self._change(failed, failed.state, dead.state, worker, now)))
            return steps, None
        retry = failed.transition(ExecutionState.RETRY_SCHEDULED)
        steps.append((retry, self._change(failed, failed.state, retry.state, worker, now)))
        delay = definition.retry.jittered_backoff(failed.attempts, self._jitter())
        if retry_after is not None:  # a deferral (a 429, the gateway's retry_at) is never cut short
            delay = max(delay, retry_after)
        return steps, execution_job(
            retry, definition, attempt=failed.attempts + 1, run_at=now + delay
        )

    def _recover(
        self,
        definition: AutomationDefinition,
        running: AutomationExecution,
        worker: ActorRef,
        now: datetime,
    ) -> AutomationExecution | None:
        """The run of a worker that died counts as a failed attempt. The retry continues in this
        job, so no extra job is enqueued."""
        steps, _ = self._failure_steps(definition, running, LEASE_EXPIRED, worker, now)
        if not self._store.advance(running, steps):
            return None
        return steps[-1][0]

    async def _reload(self, execution_id: uuid.UUID) -> AutomationExecution:
        stored = await self._offload(lambda: self._store.load(execution_id))
        if stored is None:  # pragma: no cover - executions are never deleted
            raise JobError(f"no execution {execution_id}")
        return stored.execution

    @staticmethod
    def _change(  # noqa: PLR0913, PLR0917 - one argument per column of the state change
        execution: AutomationExecution,
        from_state: ExecutionState | None,
        to_state: ExecutionState,
        actor: ActorRef,
        at: datetime,
        reason: str | None = None,
    ) -> StateChange:
        if execution.organization_id is None:  # pragma: no cover - refused at the trigger
            raise ValueError("a durable execution belongs to an organization")
        return StateChange(
            execution_id=execution.id,
            organization_id=execution.organization_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            correlation_id=execution.correlation_id,
            at=at,
            reason=reason,
        )


def _json(payload: Mapping[str, object]) -> dict[str, JsonValue]:
    """Trigger payloads are JSON objects (IDs and parameters); anything else is refused."""
    converted: dict[str, JsonValue] = {}
    for key, value in payload.items():
        if not isinstance(value, str | int | float | bool | list | dict) and value is not None:
            raise AutomationRefused(f"payload field {key!r} is not JSON")
        converted[key] = value
    return converted


def _retry_after(error: Exception) -> timedelta | None:
    """The wait an error asks for (`retry_after_seconds`, as a rate limit or the gateway's
    deferral carries it; Codex review of #108), or None."""
    seconds = getattr(error, "retry_after_seconds", None)
    if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds <= 0:
        return None
    return timedelta(seconds=float(seconds))
