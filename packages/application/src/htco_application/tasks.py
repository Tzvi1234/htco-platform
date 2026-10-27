"""Business tasks (slice 7.1, FR-TASK-01 to FR-TASK-04): one task model for every actor.

- **Creating** (`TaskService.create`): a task starts `CREATED`, with its first event and
  `tasks.created.v1` in one transaction. Creating it again with the same idempotency key returns
  the first task and writes nothing (AC-7.1.2).
- **Moving** (`TaskService.move`): one recorded transition of the domain machine, with its event
  and `tasks.state_changed.v1` in one transaction; a task that moved meanwhile is refused
  (`StaleTask`). `COMPLETED` needs the evidence it references (AC-7.1.3).
- **Reading** (`list_tasks`, `task_detail`): "View tasks" decides who sees what. Owner and Admin
  see the organization; shop roles see their granted shops' tasks (an organization-wide task only
  for Owner and Admin). A task the caller may not see does not exist for them.

Events carry IDs, the kind and the states only (EVENT_CATALOG rule 3): never the title.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.kernel import (
    DomainEvent,
    JsonValue,
    Offload,
    OutboxMessage,
    publish,
    run_inline,
)
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorRef
from htco_domain.status import status_kind
from htco_domain.tasks import BusinessTask, TaskEvidence, TaskKind, TaskState

TASK_CREATED = "tasks.created.v1"
TASK_STATE_CHANGED = "tasks.state_changed.v1"
MAX_PAGE = 200


class StaleTask(Exception):  # noqa: N818 - named like StaleIncident
    """The task moved meanwhile: reload it and try again."""


class TaskNotVisibleError(Exception):
    """No such task for the caller."""


@dataclass(frozen=True)
class TaskStep:
    """One recorded transition (the row of `app.task_events`)."""

    task_id: uuid.UUID
    organization_id: uuid.UUID
    from_state: TaskState | None
    to_state: TaskState
    actor: ActorRef
    at: datetime
    evidence: TaskEvidence | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class StoredTask:
    task: BusinessTask
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TaskDetail:
    stored: StoredTask
    history: tuple[TaskStep, ...]


@dataclass(frozen=True)
class TaskPage:
    items: tuple[StoredTask, ...]
    # The last task of a full page: pass it back as `before` for the next page.
    next_before: uuid.UUID | None


class TaskStore(Protocol):
    """Synchronous; the service offloads it."""

    def create(
        self, stored: StoredTask, step: TaskStep, events: tuple[OutboxMessage, ...]
    ) -> tuple[StoredTask, bool]:
        """In one transaction, unless the organization has a task with this idempotency key:
        insert the task, its first step and `events`. Returns the task (the first one on a
        repeat) and whether this call created it."""
        ...

    def get(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> StoredTask | None: ...

    def move(
        self,
        before: StoredTask,
        after: StoredTask,
        step: TaskStep,
        events: tuple[OutboxMessage, ...],
    ) -> StoredTask:
        """In one transaction, only while the stored task is still in `before`'s state: write
        `after`'s state and evidence, the step and `events`; else raise `StaleTask`."""
        ...


class TaskReader(Protocol):
    """Synchronous; run in a worker thread."""

    def list_tasks(  # noqa: PLR0913 - the scope, the filters and the page
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        shop_ids: frozenset[uuid.UUID] | None = None,
        states: frozenset[TaskState] | None = None,
        kind: TaskKind | None = None,
        before: uuid.UUID | None = None,
    ) -> Sequence[StoredTask]:
        """Newest first (`created_at`, then `id`); every filter before `limit`. `shop_ids` (a
        shop-scoped role's grants) leaves out organization-wide tasks."""
        ...

    def task_detail(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> TaskDetail | None: ...


def _event(name: str, task: BusinessTask, step: TaskStep, key: str) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "task_id": str(task.id),
        "kind": task.kind.value,
        "from_state": None if step.from_state is None else step.from_state.value,
        "to_state": step.to_state.value,
        "status": status_kind(step.to_state).value,
        "assignee": str(task.assignee),
        "evidence_kind": None if step.evidence is None else step.evidence.kind.value,
    }
    return publish(
        DomainEvent(
            event_type=name,
            organization_id=task.organization_id,
            shop_id=task.shop_id,
            idempotency_key=key,
            occurred_at=step.at,
            actor=step.actor,
            payload=payload,
            correlation_id=task.correlation_id,
        )
    )


@dataclass
class TaskService:
    store: TaskStore
    clock: Callable[[], datetime]
    new_id: Callable[[], uuid.UUID] = uuid.uuid7
    offload: Offload = run_inline

    async def create(  # noqa: PLR0913 - one task's facts
        self,
        *,
        organization_id: uuid.UUID,
        kind: TaskKind,
        title: str,
        assignee: ActorRef,
        idempotency_key: str,
        correlation_id: str,
        actor: ActorRef,
        shop_id: uuid.UUID | None = None,
        owner: ActorRef | None = None,
        due_at: datetime | None = None,
    ) -> tuple[StoredTask, bool]:
        now = self.clock()
        task = BusinessTask(
            id=self.new_id(), organization_id=organization_id, shop_id=shop_id, kind=kind,
            title=title, assignee=assignee, state=TaskState.CREATED,
            idempotency_key=idempotency_key, correlation_id=correlation_id, owner=owner,
            due_at=due_at,
        )  # fmt: skip
        step = TaskStep(task.id, organization_id, None, TaskState.CREATED, actor, now,
                        correlation_id=correlation_id)  # fmt: skip
        event = _event(TASK_CREATED, task, step, f"{TASK_CREATED}:{task.id}")
        stored = StoredTask(task, created_at=now, updated_at=now)
        result: tuple[StoredTask, bool] = await self.offload(
            lambda: self.store.create(stored, step, (event,))
        )
        return result

    async def move(
        self,
        organization_id: uuid.UUID,
        task_id: uuid.UUID,
        target: TaskState,
        *,
        actor: ActorRef,
        evidence: TaskEvidence | None = None,
    ) -> StoredTask:
        """Raises `TaskNotVisibleError` (no such task), `IllegalTransition`,
        `MissingEvidenceError` or `StaleTask`."""
        before = await self.offload(lambda: self.store.get(organization_id, task_id))
        if before is None:
            raise TaskNotVisibleError(str(task_id))
        now = self.clock()
        moved = before.task.transition(target, evidence=evidence)
        step = TaskStep(task_id, organization_id, before.task.state, target, actor, now,
                        evidence=evidence, correlation_id=before.task.correlation_id)  # fmt: skip
        event = _event(
            TASK_STATE_CHANGED, moved, step, f"{TASK_STATE_CHANGED}:{task_id}:{uuid.uuid7()}"
        )
        after = StoredTask(moved, created_at=before.created_at, updated_at=now)
        result: StoredTask = await self.offload(
            lambda: self.store.move(before, after, step, (event,))
        )
        return result


# --- Reading ----------------------------------------------------------------------------------


def _organization(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return organization


def _visible(
    auth: AuthContext, organization: uuid.UUID, shop: uuid.UUID | None, *, now: datetime
) -> bool:
    principal = auth.principal_for(organization)  # NoMembershipError for a stranger
    overall = decide(principal, Capability.VIEW_TASKS, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        return False
    if shop is None:
        return overall.scope is not Scope.GRANTED_SHOPS
    scoped = decide(
        principal, Capability.VIEW_TASKS, shop=ShopRef(id=shop, organization_id=organization),
        now=now,
    )  # fmt: skip
    return scoped.outcome is Outcome.ALLOW


def list_tasks(  # noqa: PLR0913 - the caller, the reader and the filters
    auth: AuthContext,
    reader: TaskReader,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
    limit: int = MAX_PAGE,
    states: frozenset[TaskState] | None = None,
    kind: TaskKind | None = None,
    before: uuid.UUID | None = None,
) -> TaskPage:
    """One page, newest first. Raises `PermissionError` for a role that may not view tasks."""
    if not 0 < limit <= MAX_PAGE:
        raise ValueError(f"a page holds 1 to {MAX_PAGE} tasks")
    organization = _organization(auth, organization_id)
    principal = auth.principal_for(organization)
    overall = decide(principal, Capability.VIEW_TASKS, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not view tasks")
    shops = principal.granted_shops if overall.scope is Scope.GRANTED_SHOPS else None
    rows = reader.list_tasks(
        organization, limit=limit + 1, shop_ids=shops, states=states, kind=kind, before=before
    )
    page = tuple(rows[:limit])
    more = len(rows) > limit
    return TaskPage(
        items=tuple(t for t in page if _visible(auth, organization, t.task.shop_id, now=now)),
        next_before=page[-1].task.id if more and page else None,
    )


def task_detail(
    auth: AuthContext,
    reader: TaskReader,
    task_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> TaskDetail:
    organization = _organization(auth, organization_id)
    # Membership and the role first, before anything is read (sage-arch on #181): a stranger or
    # a role without the view never reaches the store, and learns nothing about the ID.
    try:
        principal = auth.principal_for(organization)
    except NoMembershipError:
        raise TaskNotVisibleError(str(task_id)) from None
    if decide(principal, Capability.VIEW_TASKS, shop=None, now=now).outcome is not Outcome.ALLOW:
        raise TaskNotVisibleError(str(task_id))
    detail = reader.task_detail(organization, task_id)
    if detail is None or not _visible(auth, organization, detail.stored.task.shop_id, now=now):
        raise TaskNotVisibleError(str(task_id))
    return detail
