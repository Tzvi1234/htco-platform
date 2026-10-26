"""Business tasks: units of work with business states, owned by people, employees or automations.

A task is a CRM record, independent of any agent runtime: a finished AI conversation is not
proof that its task completed (FR-TASK-01, FR-TASK-02).

**One task model (AUD-03, slice 7.1).** All work for a person, an employee or an automation is a
`BusinessTask` of some `TaskKind`: the Human channel's hand-off, the AutoVA relay item (6.10) and
an approval's follow-up are kinds, never their own states or tables. A kind adds data; the states
are always `TaskState`.

**Completion on evidence (AC-7.1.3).** A task reaches `COMPLETED` only through a transition that
names its `TaskEvidence`: a resource, an execution or an approval. No message or conversation
text is evidence.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from htco_domain.actors import ActorRef
from htco_domain.state_machine import check_transition


class TaskState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_FOR_EXTERNAL = "waiting_for_external"
    WAITING_FOR_AGENT = "waiting_for_agent"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETRY_SCHEDULED = "retry_scheduled"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


_S = TaskState
_WAITING = frozenset({_S.WAITING_FOR_EXTERNAL, _S.WAITING_FOR_AGENT, _S.WAITING_FOR_APPROVAL})
_TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    _S.CREATED: frozenset({_S.QUEUED, _S.CANCELLED}),
    _S.QUEUED: frozenset({_S.CLAIMED, _S.CANCELLED}),
    # A claim can lapse (expired lease): the task goes back to the queue.
    _S.CLAIMED: frozenset({_S.RUNNING, _S.QUEUED}),
    _S.RUNNING: _WAITING
    | frozenset({_S.COMPLETED, _S.FAILED, _S.RETRY_SCHEDULED, _S.BLOCKED, _S.ESCALATED}),
    **{
        waiting: frozenset({_S.RUNNING, _S.QUEUED, _S.CANCELLED, _S.ESCALATED})
        for waiting in _WAITING
    },
    _S.RETRY_SCHEDULED: frozenset({_S.QUEUED, _S.CANCELLED}),
    _S.BLOCKED: frozenset({_S.QUEUED, _S.ESCALATED, _S.CANCELLED}),
    _S.FAILED: frozenset({_S.RETRY_SCHEDULED, _S.ESCALATED}),
    _S.ESCALATED: frozenset({_S.QUEUED, _S.CANCELLED}),
}


MAX_KEY_LENGTH = 200


class TaskKind(StrEnum):
    GENERAL = "general"  # office work: a person's or an employee's task
    HUMAN_CHANNEL = "human_channel"  # an action handed to a person (login, challenge, refund)
    RELAY = "relay"  # an AutoVA tracking relay item (slice 6.10)
    APPROVAL_FOLLOW_UP = "approval_follow_up"  # the work an approval leaves behind


class EvidenceKind(StrEnum):
    RESOURCE = "resource"  # a record that shows the work is done (an order, a listing)
    EXECUTION = "execution"  # an automation or action execution
    APPROVAL = "approval"


@dataclass(frozen=True)
class TaskEvidence:
    kind: EvidenceKind
    reference: str  # the resource, execution or approval it names; never free text

    def __post_init__(self) -> None:
        if not self.reference.strip() or len(self.reference) > MAX_KEY_LENGTH:
            raise ValueError(f"evidence names its reference in 1 to {MAX_KEY_LENGTH} characters")


class MissingEvidenceError(ValueError):
    """A task completes only with the evidence of its work (AC-7.1.3)."""


@dataclass(frozen=True)
class BusinessTask:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    kind: TaskKind
    title: str
    assignee: ActorRef
    state: TaskState
    # Creating the same task again returns the first one (AC-7.1.2).
    idempotency_key: str
    correlation_id: str
    owner: ActorRef | None = None  # who is accountable, when it is not the assignee
    due_at: datetime | None = None  # the SLA
    evidence: TaskEvidence | None = None  # set by the transition to COMPLETED

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a task needs a title")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > MAX_KEY_LENGTH:
            raise ValueError(f"a task's idempotency key is 1 to {MAX_KEY_LENGTH} characters")
        if not 0 < len(self.correlation_id) <= MAX_KEY_LENGTH:
            raise ValueError(f"a task's correlation ID is 1 to {MAX_KEY_LENGTH} characters")
        if self.due_at is not None and self.due_at.tzinfo is None:
            raise ValueError("a task is due at a timezone-aware time")

    def transition(
        self, target: TaskState, *, evidence: TaskEvidence | None = None
    ) -> BusinessTask:
        check_transition("task", _TASK_TRANSITIONS, self.state, target)
        if target is TaskState.COMPLETED:
            if evidence is None:
                raise MissingEvidenceError("a task completes only with its evidence")
            return replace(self, state=target, evidence=evidence)
        if evidence is not None:
            raise ValueError("only completing a task records evidence")
        return replace(self, state=target)
