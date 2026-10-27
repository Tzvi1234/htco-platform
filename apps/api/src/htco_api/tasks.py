"""Tasks (slice 7.1): read-only views of the one task model.

- `GET /v1/tasks`: the organization's tasks the caller may view, newest first, in cursor pages,
  optionally by state and kind.
- `GET /v1/tasks/{id}`: one task with its recorded steps.

"View tasks" decides who reads a task (Owner and Admin the organization, other roles their granted
shops); a task the caller may not see does not exist for them. Tasks move through the service
(the Human channel, the relay, approvals), never through this API in 7.1.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import (
    ORGANIZATION_REQUIRED,
    decode_cursor,
    encode_cursor,
    problem_responses,
)
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, Problem, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.shops import OrganizationRequiredError
from htco_application.tasks import (
    StoredTask,
    TaskDetail,
    TaskNotVisibleError,
    TaskReader,
    list_tasks,
    task_detail,
)
from htco_domain.status import status_kind
from htco_domain.tasks import TaskKind, TaskState

TASKS_UNAVAILABLE = ProblemType(
    "tasks-unavailable", "Tasks not available", 503, "Tasks cannot be read on this server."
)


@dataclass(frozen=True)
class TaskCenter:
    reader: TaskReader


class TaskActorView(BaseModel):
    kind: str
    id: str


class TaskSummaryView(BaseModel):
    id: uuid.UUID
    shop_id: uuid.UUID | None
    kind: TaskKind
    title: str
    state: TaskState
    status: str  # the shared status kind: active, waiting, succeeded, failed or stopped
    assignee: TaskActorView
    owner: TaskActorView | None
    due_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskListView(BaseModel):
    items: list[TaskSummaryView]
    # Pass it back as `cursor` for the next page; null on the last page.
    next_cursor: str | None = None


class TaskEvidenceView(BaseModel):
    kind: str
    reference: str


class TaskStepView(BaseModel):
    from_state: TaskState | None
    to_state: TaskState
    actor: TaskActorView
    at: datetime
    evidence: TaskEvidenceView | None


class TaskDetailView(TaskSummaryView):
    evidence: TaskEvidenceView | None
    history: list[TaskStepView]


def _center(request: Request) -> TaskCenter:
    found = getattr(request.app.state, "task_center", None)
    if found is None:
        raise ProblemError(TASKS_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


def _summary(stored: StoredTask) -> dict[str, Any]:
    t = stored.task
    return {
        "id": t.id, "shop_id": t.shop_id, "kind": t.kind, "title": t.title, "state": t.state,
        "status": status_kind(t.state).value,
        "assignee": TaskActorView(kind=t.assignee.kind.value, id=t.assignee.id),
        "owner": None if t.owner is None else TaskActorView(kind=t.owner.kind.value, id=t.owner.id),
        "due_at": t.due_at, "created_at": stored.created_at, "updated_at": stored.updated_at,
    }  # fmt: skip


def _detail(detail: TaskDetail) -> TaskDetailView:
    evidence = detail.stored.task.evidence
    return TaskDetailView(
        **_summary(detail.stored),
        evidence=None
        if evidence is None
        else TaskEvidenceView(kind=evidence.kind.value, reference=evidence.reference),
        history=[
            TaskStepView(
                from_state=s.from_state, to_state=s.to_state,
                actor=TaskActorView(kind=s.actor.kind.value, id=s.actor.id), at=s.at,
                evidence=None if s.evidence is None
                else TaskEvidenceView(kind=s.evidence.kind.value, reference=s.evidence.reference),
            )
            for s in detail.history
        ],
    )  # fmt: skip


_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    400: "The caller belongs to several organizations and named none",
    422: ("Invalid ID, filter or organization", ValidationProblem),
    503: "Tasks cannot be read on this server (tasks-unavailable)",
}


def tasks_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["tasks"])
    Center = Annotated[TaskCenter, Depends(_center)]  # noqa: N806 - a type alias

    @router.get(
        "/tasks",
        summary="The organization's tasks the caller may view",
        responses=problem_responses({**_COMMON, 403: "The role may not view tasks"}),
    )
    async def get_tasks(  # noqa: PLR0913, PLR0917 - the filters and the page
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
        state: Annotated[list[TaskState] | None, Query(max_length=13)] = None,
        kind: TaskKind | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
    ) -> TaskListView:
        """Newest first; a shop-scoped role sees its granted shops' tasks only. `state` may be
        repeated; pass `next_cursor` back as `cursor` for the next page."""
        before = decode_cursor(cursor) if cursor is not None else None
        try:
            page = await anyio.to_thread.run_sync(
                lambda: list_tasks(
                    auth, center.reader, organization_id=organization_id, now=clock(),
                    limit=limit, states=None if not state else frozenset(state), kind=kind,
                    before=before,
                )
            )  # fmt: skip
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError, NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return TaskListView(
            items=[TaskSummaryView(**_summary(t)) for t in page.items],
            next_cursor=None if page.next_before is None else encode_cursor(page.next_before),
        )

    @router.get(
        "/tasks/{task_id}",
        summary="One task with its recorded steps",
        responses=problem_responses({**_COMMON, 404: "No such task for the caller"}),
    )
    async def get_task(
        task_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> TaskDetailView:
        try:
            detail = await anyio.to_thread.run_sync(
                lambda: task_detail(
                    auth, center.reader, task_id, organization_id=organization_id, now=clock()
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except TaskNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        response.headers["Cache-Control"] = "no-store"
        return _detail(detail)

    return router
