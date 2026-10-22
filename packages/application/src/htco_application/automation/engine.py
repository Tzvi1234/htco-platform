"""The workflow execution engine of the Automation Department (ADR-0013).

Every trigger (an external event, a schedule, an authorized agent tool, a user action or
another workflow) enters through `trigger`, which checks the automation's readiness, the
trigger kind and, for employees, the employee's automation grants. An execution is keyed by
`(automation_id, idempotency_key)`: a replayed trigger returns the existing execution and never
runs the business action again. Failures follow the automation's retry policy and end in the
dead-letter state. This version runs a handler in-process against an `ExecutionStore` port;
durable queuing (inbox, outbox, leases) arrives with the job queue in Phase 5.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AutomationDefinition,
    AutomationExecution,
    ExecutionState,
    TriggerKind,
)
from htco_domain.workforce import PRODUCTION_READINESS

type Handler = Callable[[Mapping[str, object]], Mapping[str, object]]


class ExecutionStore(Protocol):
    def find(self, automation_id: str, idempotency_key: str) -> AutomationExecution | None: ...

    def save(self, execution: AutomationExecution) -> None: ...


class AutomationRefused(Exception):  # noqa: N818 - reads as "automation (was) refused"
    pass


@dataclass(frozen=True)
class TriggerRequest:
    automation_id: str
    trigger: TriggerKind
    actor: ActorRef
    idempotency_key: str
    payload: Mapping[str, object]
    correlation_id: str
    # The tenant (durable engine, slice 5.10) and what caused the trigger.
    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    causation_id: str | None = None


def authorize(
    registry: Registry, handlers: Mapping[str, object], request: TriggerRequest
) -> AutomationDefinition:
    """The single gate for every trigger (ADR-0013): known, production-ready, deployed, callable
    from this trigger kind, and for employees granted. Raises `AutomationRefused` with the reason,
    before anything is written (AC-5.10.2)."""
    definition = registry.automations.get(request.automation_id)
    if definition is None:
        raise AutomationRefused(f"unknown automation {request.automation_id!r}")
    if definition.readiness not in PRODUCTION_READINESS:
        raise AutomationRefused(f"{definition.id} is not active ({definition.readiness.value})")
    if definition.id not in handlers:
        raise AutomationRefused(f"{definition.id} has no handler deployed")
    if request.trigger not in definition.callable_from:
        raise AutomationRefused(f"{definition.id} cannot be triggered by {request.trigger.value}")
    if request.actor.kind is ActorKind.EMPLOYEE:
        _authorize_employee(registry, definition, request)
    return definition


def _authorize_employee(
    registry: Registry, definition: AutomationDefinition, request: TriggerRequest
) -> None:
    employee = request.actor.id
    if request.trigger is not TriggerKind.AGENT_TOOL:
        raise AutomationRefused("employees trigger automations only through agent_tool")
    profile = registry.employees.get(employee)
    if profile is None:
        raise AutomationRefused(f"unknown employee {employee!r}")
    if not profile.is_production_ready():
        raise AutomationRefused(f"{employee} is not active ({profile.readiness.value})")
    if definition.id not in profile.allowed_automations:
        raise AutomationRefused(f"{definition.id} is not granted to {employee}")


class AutomationEngine:
    def __init__(
        self,
        registry: Registry,
        store: ExecutionStore,
        handlers: Mapping[str, Handler],
        new_id: Callable[[], uuid.UUID],
    ) -> None:
        self._registry = registry
        self._store = store
        self._handlers = handlers
        self._new_id = new_id
        self._payloads: dict[tuple[str, str], Mapping[str, object]] = {}

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        definition = self._authorize(request)
        existing = self._store.find(request.automation_id, request.idempotency_key)
        if existing is not None:
            return existing
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
        )
        self._store.save(execution)
        self._payloads[(definition.id, request.idempotency_key)] = request.payload
        return self._run(definition, execution)

    def retry(self, automation_id: str, idempotency_key: str) -> AutomationExecution:
        """Run a scheduled retry (called by the scheduler when the backoff has passed)."""
        execution = self._store.find(automation_id, idempotency_key)
        if execution is None or execution.state is not ExecutionState.RETRY_SCHEDULED:
            raise AutomationRefused(f"{automation_id}: nothing to retry for {idempotency_key}")
        return self._run(self._registry.automations[automation_id], execution)

    def _authorize(self, request: TriggerRequest) -> AutomationDefinition:
        return authorize(self._registry, self._handlers, request)

    def _run(
        self, definition: AutomationDefinition, execution: AutomationExecution
    ) -> AutomationExecution:
        running = execution.transition(ExecutionState.RUNNING)
        self._store.save(running)
        payload = self._payloads.get((definition.id, execution.idempotency_key), {})
        try:
            self._handlers[definition.id](payload)
        except Exception:
            failed = running.transition(ExecutionState.FAILED)
            next_state = (
                ExecutionState.RETRY_SCHEDULED
                if failed.attempts < definition.retry.max_attempts
                else ExecutionState.DEAD_LETTERED
            )
            finished = failed.transition(next_state)
        else:
            finished = running.transition(ExecutionState.SUCCEEDED)
        self._store.save(finished)
        return finished
