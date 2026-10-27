"""The activity query (slice 7.8, FR-AUD-04): what the office did, from recorded activity only.

"View agent runs" decides who reads activity records: Owner and Admin the whole organization,
Operations and Developer their granted shops' records (an organization-wide record only for
Owner and Admin), as the RLS policy of slice 7.7. The shop scope goes into the query, before the
limit. A page names its source and when it was read, and an empty page says "no recorded
activity", never an inferred state (AC-7.8.4).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide
from htco_application.shops import OrganizationRequiredError
from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef

ACTIVITY_SOURCE = "app.activity_records"
NO_RECORDED_ACTIVITY = "no_recorded_activity"
MAX_PAGE = 200


def day_range(day: date, timezone: str) -> tuple[datetime, datetime]:
    """The day in a time zone, as a UTC range [start, end)."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time(), zone)
    end = datetime.combine(day + timedelta(days=1), time(), zone)
    utc = ZoneInfo("UTC")
    return start.astimezone(utc), end.astimezone(utc)


@dataclass(frozen=True)
class ActivityFilter:
    actor: ActorRef | None = None
    actor_kind: ActorKind | None = None
    shop_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    kind: ActivityKind | None = None
    outcome: ActivityOutcome | None = None
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive

    def __post_init__(self) -> None:
        for bound in (self.since, self.until):
            if bound is not None and bound.tzinfo is None:
                raise ValueError("a time bound needs a timezone")
        if self.since is not None and self.until is not None and self.until < self.since:
            raise ValueError("the range ends before it starts")

    def matches(self, r: ActivityRecord) -> bool:
        """The filter's meaning, for in-memory readers and tests (Postgres applies it in SQL)."""
        return (
            (self.actor is None or r.actor == self.actor)
            and (self.actor_kind is None or r.actor.kind is self.actor_kind)
            and (self.shop_id is None or r.shop_id == self.shop_id)
            and (self.task_id is None or r.task_id == self.task_id)
            and (self.kind is None or r.kind is self.kind)
            and (self.outcome is None or r.outcome is self.outcome)
            and (self.since is None or r.occurred_at >= self.since)
            and (self.until is None or r.occurred_at < self.until)
        )


@dataclass(frozen=True)
class ActivityCursor:
    """Keyset paging: the last record of a page (newest first)."""

    occurred_at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class ActivityPage:
    items: tuple[ActivityRecord, ...]
    next_before: ActivityCursor | None
    source: str
    read_at: datetime
    # A task's trail reads oldest first: where its next page starts, None on the last page
    # (quill2 on #200: a long trail is never cut silently).
    next_after: ActivityCursor | None = None

    @property
    def empty_reason(self) -> str | None:
        return NO_RECORDED_ACTIVITY if not self.items else None


class ActivityReader(Protocol):
    """Synchronous; run in a worker thread. `shop_ids` (a shop-scoped role's grants) leaves out
    organization-wide records; every filter is applied before `limit`."""

    def list_activity(
        self,
        organization_id: uuid.UUID,
        *,
        where: ActivityFilter,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
        before: ActivityCursor | None = None,
    ) -> Sequence[ActivityRecord]:
        """Newest first (`occurred_at`, then `id`)."""
        ...

    def task_trail(
        self,
        organization_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
        after: ActivityCursor | None = None,
    ) -> Sequence[ActivityRecord]:
        """The task's records and every record sharing their correlation IDs, oldest first,
        after `after` when given."""
        ...


def _scope(
    auth: AuthContext, organization_id: uuid.UUID | None, now: datetime
) -> tuple[uuid.UUID, frozenset[uuid.UUID] | None]:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)  # NoMembershipError for a stranger
    decision = decide(principal, Capability.VIEW_AGENT_RUNS, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not view activity")
    shops = principal.granted_shops if decision.scope is Scope.GRANTED_SHOPS else None
    return organization, shops


def list_activity(  # noqa: PLR0913 - the caller, the reader and the query
    auth: AuthContext,
    reader: ActivityReader,
    *,
    organization_id: uuid.UUID | None,
    where: ActivityFilter,
    now: datetime,
    limit: int = MAX_PAGE,
    before: ActivityCursor | None = None,
) -> ActivityPage:
    """One page, newest first. Raises `PermissionError` for a role that may not view activity."""
    organization, shops = _scope(auth, organization_id, now)
    limit = max(1, min(limit, MAX_PAGE))
    rows = tuple(
        reader.list_activity(
            organization, where=where, shop_ids=shops, limit=limit + 1, before=before
        )
    )
    page = rows[:limit]
    last = page[-1] if len(rows) > limit else None
    return ActivityPage(
        items=page,
        next_before=None if last is None else ActivityCursor(last.occurred_at, last.id),
        source=ACTIVITY_SOURCE,
        read_at=now,
    )


def task_trail(  # noqa: PLR0913 - the caller, the reader and the task
    auth: AuthContext,
    reader: ActivityReader,
    task_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
    limit: int = MAX_PAGE,
    after: ActivityCursor | None = None,
) -> ActivityPage:
    """A task's trail across actors, in causal (recorded) order, one page at a time: every
    record is reached by following `next_after` (AC-7.8.3)."""
    organization, shops = _scope(auth, organization_id, now)
    limit = max(1, min(limit, MAX_PAGE))
    # One more than the page says whether the trail continues.
    rows = tuple(
        reader.task_trail(organization, task_id, shop_ids=shops, limit=limit + 1, after=after)
    )
    page = rows[:limit]
    last = page[-1] if len(rows) > limit else None
    return ActivityPage(
        items=page,
        next_before=None,
        source=ACTIVITY_SOURCE,
        read_at=now,
        next_after=None if last is None else ActivityCursor(last.occurred_at, last.id),
    )
