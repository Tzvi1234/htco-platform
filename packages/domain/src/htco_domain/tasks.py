"""Business tasks: units of work with business states, owned by people, employees or automations.

A task is a CRM record, independent of any agent runtime: a finished AI conversation is not
proof that its task completed (FR-TASK-01, FR-TASK-02).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class BusinessTask:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    title: str
    assignee: ActorRef
    state: TaskState
    correlation_id: str

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a task needs a title")

    def transition(self, target: TaskState) -> BusinessTask:
        check_transition("task", _TASK_TRANSITIONS, self.state, target)
        return replace(self, state=target)
