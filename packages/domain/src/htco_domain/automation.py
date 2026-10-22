"""The Automation Department: deterministic workflows and their executions (ADR-0013).

Automations are not agents. They run business actions from events, schedules, authorized
agent tools, user actions or other workflows, without model calls unless a named step truly
needs reasoning (`ai_steps`). Every execution carries an idempotency key, so a replayed trigger
never repeats a business action.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum

from htco_domain.actors import ActorRef
from htco_domain.state_machine import check_transition
from htco_domain.tooling import SideEffect
from htco_domain.workforce import PRODUCTION_READINESS, Readiness, SpecSection, check_specification


class TriggerKind(StrEnum):
    EVENT = "event"
    SCHEDULE = "schedule"
    AGENT_TOOL = "agent_tool"
    USER_ACTION = "user_action"
    WORKFLOW = "workflow"


# The versioned specification every automation carries.
AUTOMATION_SPEC_SECTIONS: tuple[str, ...] = (
    "identity",
    "business_owner",
    "trigger",
    "input_schema",
    "preconditions",
    "steps",
    "output_schema",
    "dependencies",
    "error_handling",
    "retry_policy",
    "idempotency",
    "timeouts_and_sla",
    "permissions",
    "audit",
    "tests",
    "recovery",
    "approvals",
    "metrics",
)

_AUTOMATION_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_backoff: timedelta
    max_backoff: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("a retry policy needs at least one attempt")
        if self.initial_backoff < timedelta(0) or self.max_backoff < self.initial_backoff:
            raise ValueError("backoff must be non-negative and below its cap")

    def backoff(self, attempt: int) -> timedelta:
        """Delay before retrying after `attempt` failed attempts: doubling, capped."""
        delay: timedelta = self.initial_backoff * 2 ** (attempt - 1)
        return min(delay, self.max_backoff)

    def jittered_backoff(self, attempt: int, jitter: float) -> timedelta:
        """The backoff with "equal jitter": between half and all of `backoff(attempt)`, so retries
        of many failed jobs spread out instead of arriving together (ER-REL-03). `jitter` is a
        random number in [0, 1], injected so tests are deterministic."""
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be between 0 and 1")
        half = self.backoff(attempt) / 2
        return half + half * jitter


@dataclass(frozen=True)
class AutomationDefinition:
    id: str
    name: str
    version: int
    business_owner: str  # an employee id, or "owner"
    primary_trigger: TriggerKind
    callable_from: frozenset[TriggerKind]
    retry: RetryPolicy
    timeout: timedelta
    side_effect: SideEffect
    approval_required: bool
    ai_steps: tuple[str, ...]  # steps that call a model; empty for a fully deterministic flow
    readiness: Readiness
    specification: Mapping[str, SpecSection]
    activation_phase: int  # the plan phase that builds and activates it

    def __post_init__(self) -> None:
        if self.activation_phase < 0:
            raise ValueError("activation_phase must be a plan phase (0 or more)")
        if not _AUTOMATION_ID.match(self.id):
            raise ValueError(f"invalid automation id {self.id!r} (expected kebab-case)")
        if self.version < 1:
            raise ValueError("automation version starts at 1")
        if self.timeout <= timedelta(0):
            raise ValueError("an automation needs a positive timeout")
        if self.primary_trigger not in self.callable_from:
            raise ValueError("the primary trigger must be one of callable_from")
        drafts = check_specification(self.specification, AUTOMATION_SPEC_SECTIONS)
        if self.readiness in PRODUCTION_READINESS and drafts:
            raise ValueError(f"{self.id}: cannot be {self.readiness.value} with draft sections")


class ExecutionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


_EXECUTION_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PENDING: frozenset({ExecutionState.RUNNING, ExecutionState.CANCELLED}),
    ExecutionState.RUNNING: frozenset({ExecutionState.SUCCEEDED, ExecutionState.FAILED}),
    ExecutionState.FAILED: frozenset(
        {ExecutionState.RETRY_SCHEDULED, ExecutionState.DEAD_LETTERED, ExecutionState.COMPENSATED}
    ),
    ExecutionState.RETRY_SCHEDULED: frozenset({ExecutionState.RUNNING, ExecutionState.CANCELLED}),
}
_TERMINAL = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.DEAD_LETTERED,
        ExecutionState.COMPENSATED,
        ExecutionState.CANCELLED,
    }
)


@dataclass(frozen=True)
class AutomationExecution:
    id: uuid.UUID
    automation_id: str
    automation_version: int
    idempotency_key: str
    trigger: TriggerKind
    actor: ActorRef
    state: ExecutionState
    attempts: int
    correlation_id: str

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("an execution needs an idempotency key")

    def transition(self, target: ExecutionState) -> AutomationExecution:
        check_transition("automation execution", _EXECUTION_TRANSITIONS, self.state, target)
        attempts = self.attempts + 1 if target is ExecutionState.RUNNING else self.attempts
        return replace(self, state=target, attempts=attempts)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL
