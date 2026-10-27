"""The activity query (slice 7.8).

- `GET /v1/activity`: the organization's activity records the caller may view, newest first, in
  cursor pages, filtered by actor (`automation:etsy-order-intake`), actor kind, shop, task, kind,
  outcome and a time range, or a `day` in the caller's `timezone`.
- `GET /v1/tasks/{task_id}/activity`: one task's trail across actors, by correlation, oldest
  first.

Every answer names its source and when it was read; an empty one says "no recorded activity".
Identifiers, codes and template summaries only.
"""

import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import ORGANIZATION_REQUIRED, problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import Problem, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.activity_query import (
    ActivityCursor,
    ActivityFilter,
    ActivityPage,
    ActivityReader,
    day_range,
    list_activity,
    task_trail,
)
from htco_application.shops import OrganizationRequiredError
from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef

ACTIVITY_UNAVAILABLE = ProblemType(
    "activity-unavailable", "Activity not available", 503,
    "Activity records cannot be read on this server.",
)  # fmt: skip
_CURSOR_MAX = 256


class ActivityRecordView(BaseModel):
    id: uuid.UUID
    kind: str
    shop_id: uuid.UUID | None
    actor: str  # "{kind}:{id}"
    actor_kind: str
    action: str
    outcome: str
    summary: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    task_id: uuid.UUID | None


class ActivityPageView(BaseModel):
    items: list[ActivityRecordView]
    # Pass it back as `cursor` for the next page; null on the last page.
    next_cursor: str | None = None
    source: str
    read_at: datetime
    # "no_recorded_activity" when there are no items: never an inferred state (AC-7.8.4).
    empty_reason: str | None


def _invalid(name: str, message: str) -> RequestValidationError:
    return RequestValidationError([{"loc": ("query", name), "msg": message, "type": "value_error"}])


def _encode(cursor: ActivityCursor) -> str:
    raw = f"{cursor.occurred_at.isoformat()}|{cursor.id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(cursor: str) -> ActivityCursor:
    invalid = _invalid("cursor", "Invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, _, ident = raw.partition("|")
        decoded = ActivityCursor(datetime.fromisoformat(at), uuid.UUID(ident))
    except binascii.Error, UnicodeDecodeError, ValueError:
        raise invalid from None
    if decoded.occurred_at.tzinfo is None:
        raise invalid
    return decoded


def _actor(text: str) -> ActorRef:
    kind, _, ident = text.partition(":")
    invalid = _invalid("actor", "An actor is kind:id, for example employee:shon")
    if not ident:
        raise invalid
    try:
        return ActorRef(ActorKind(kind), ident)
    except ValueError:
        raise invalid from None


def _filter(  # noqa: PLR0913 - one argument per query parameter
    *,
    actor: str | None,
    actor_kind: ActorKind | None,
    shop_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    kind: ActivityKind | None,
    outcome: ActivityOutcome | None,
    since: datetime | None,
    until: datetime | None,
    day: date | None,
    timezone: str,
) -> ActivityFilter:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        raise _invalid("timezone", "Unknown time zone") from None
    if day is not None:
        if since is not None or until is not None:
            raise _invalid("day", "Name a day or a range, not both")
        since, until = day_range(day, timezone)
    try:
        return ActivityFilter(
            actor=None if actor is None else _actor(actor), actor_kind=actor_kind,
            shop_id=shop_id, task_id=task_id, kind=kind, outcome=outcome,
            since=since, until=until,
        )  # fmt: skip
    except ValueError as error:
        raise _invalid("since", str(error)) from None


def _view(r: ActivityRecord) -> ActivityRecordView:
    return ActivityRecordView(
        id=r.id, kind=r.kind.value, shop_id=r.shop_id, actor=f"{r.actor.kind.value}:{r.actor.id}",
        actor_kind=r.actor.kind.value, action=r.action, outcome=r.outcome.value,
        summary=r.summary, occurred_at=r.occurred_at, correlation_id=r.correlation_id,
        causation_id=r.causation_id, task_id=r.task_id,
    )  # fmt: skip


def _page(page: ActivityPage) -> ActivityPageView:
    return ActivityPageView(
        items=[_view(r) for r in page.items],
        next_cursor=_next_cursor(page),
        source=page.source,
        read_at=page.read_at,
        empty_reason=page.empty_reason,
    )


def _next_cursor(page: ActivityPage) -> str | None:
    """A list continues before its last record (newest first), a trail after it (oldest first)."""
    following = page.next_before if page.next_before is not None else page.next_after
    return None if following is None else _encode(following)


def _reader(request: Request) -> ActivityReader:
    found = getattr(request.app.state, "activity_reader", None)
    if found is None:
        raise ProblemError(ACTIVITY_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    400: "The caller belongs to several organizations and named none",
    403: "The role may not view activity",
    422: ("Invalid filter, cursor or organization", ValidationProblem),
    503: "Activity cannot be read on this server (activity-unavailable)",
}


def activity_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["activity"])
    Reader = Annotated[ActivityReader, Depends(_reader)]  # noqa: N806 - a type alias

    async def _answer(response: Response, work: Callable[[], ActivityPage]) -> ActivityPageView:
        try:
            page: Any = await anyio.to_thread.run_sync(work)
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError, NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return _page(page)

    @router.get(
        "/activity",
        summary="The organization's activity records the caller may view",
        responses=problem_responses(_COMMON),
    )
    async def get_activity(  # noqa: PLR0913, PLR0917 - the filters and the page
        response: Response,
        auth: CurrentAuth,
        reader: Reader,
        organization_id: uuid.UUID | None = None,
        actor: Annotated[str | None, Query(max_length=210)] = None,
        actor_kind: ActorKind | None = None,
        shop_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        kind: ActivityKind | None = None,
        outcome: ActivityOutcome | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        day: date | None = None,
        timezone: Annotated[str, Query(max_length=64)] = "UTC",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX)] = None,
    ) -> ActivityPageView:
        """Newest first; a shop-scoped role sees its granted shops' records only. `day` is the
        caller's day in `timezone` (instead of `since`/`until`); pass `next_cursor` back as
        `cursor` for the next page."""
        where = _filter(
            actor=actor, actor_kind=actor_kind, shop_id=shop_id, task_id=task_id, kind=kind,
            outcome=outcome, since=since, until=until, day=day, timezone=timezone,
        )  # fmt: skip
        before = None if cursor is None else _decode(cursor)
        now = clock()
        return await _answer(
            response,
            lambda: list_activity(
                auth, reader, organization_id=organization_id, where=where, now=now,
                limit=limit, before=before,
            ),
        )  # fmt: skip

    @router.get(
        "/tasks/{task_id}/activity",
        summary="One task's activity trail across actors",
        responses=problem_responses(_COMMON),
    )
    async def get_task_activity(  # noqa: PLR0913, PLR0917 - the task and the page
        task_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        reader: Reader,
        organization_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
        cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX)] = None,
    ) -> ActivityPageView:
        """Every record of the task and every record sharing its correlation IDs, oldest
        first, in pages of `limit`; pass `next_cursor` back as `cursor` for the rest of the
        trail, so a long trail is never cut."""
        after = None if cursor is None else _decode(cursor)
        now = clock()
        return await _answer(
            response,
            lambda: task_trail(
                auth, reader, task_id, organization_id=organization_id, now=now, limit=limit,
                after=after,
            ),
        )  # fmt: skip

    return router
