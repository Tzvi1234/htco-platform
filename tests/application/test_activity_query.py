"""The activity query (slice 7.8): filters, the day in the user's time zone, shop grants, a task's
trail, the page's source and freshness. In memory, with the same visibility as the RLS policy of
slice 7.7. All values are invented."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.activity_query import (
    ACTIVITY_SOURCE,
    ActivityCursor,
    ActivityFilter,
    day_range,
    list_activity,
    task_trail,
)
from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role

ORG = uuid.UUID("00000000-0000-0000-0000-00000000a001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000a002")
OTHER_SHOP = uuid.UUID("00000000-0000-0000-0000-00000000a003")
USER = uuid.UUID("00000000-0000-0000-0000-00000000a0a1")
TASK = uuid.UUID("00000000-0000-0000-0000-00000000a0f1")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
INTAKE = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")
SYNC = ActorRef(ActorKind.AUTOMATION, "tracking-sync")
SHON = ActorRef(ActorKind.EMPLOYEE, "shon")


def record(  # noqa: PLR0913 - one argument per varied field
    at: datetime,
    *,
    actor: ActorRef = INTAKE,
    shop: uuid.UUID | None = SHOP,
    outcome: ActivityOutcome = ActivityOutcome.SUCCEEDED,
    kind: ActivityKind = ActivityKind.AUTOMATION_EXECUTION,
    correlation: str = "c-1",
    causation: str | None = None,
    task: uuid.UUID | None = None,
) -> ActivityRecord:
    return ActivityRecord(
        id=uuid.uuid7(), kind=kind, organization_id=ORG, shop_id=shop, actor=actor,
        action="orders.ingested", outcome=outcome, summary="Order ingested.", occurred_at=at,
        correlation_id=correlation, causation_id=causation, task_id=task,
    )  # fmt: skip


@dataclass
class MemoryActivity:
    """The reader's contract: organization, shop scope and filters before the limit."""

    rows: list[ActivityRecord] = field(default_factory=list)

    @staticmethod
    def _scoped(r: ActivityRecord, shop_ids: frozenset[uuid.UUID] | None) -> bool:
        return shop_ids is None or (r.shop_id is not None and r.shop_id in shop_ids)

    def list_activity(
        self,
        organization_id: uuid.UUID,
        *,
        where: ActivityFilter,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
        before: ActivityCursor | None = None,
    ) -> Sequence[ActivityRecord]:
        rows = [
            r for r in self.rows
            if r.organization_id == organization_id and self._scoped(r, shop_ids)
            and where.matches(r)
            and (before is None or (r.occurred_at, r.id) < (before.occurred_at, before.id))
        ]  # fmt: skip
        rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
        return rows[:limit]

    def task_trail(
        self,
        organization_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
    ) -> Sequence[ActivityRecord]:
        mine = [r for r in self.rows if r.organization_id == organization_id]
        correlations = {r.correlation_id for r in mine if r.task_id == task_id}
        rows = [
            r for r in mine
            if (r.task_id == task_id or r.correlation_id in correlations)
            and self._scoped(r, shop_ids)
        ]  # fmt: skip
        rows.sort(key=lambda r: (r.occurred_at, r.id))
        return rows[:limit]


def acting(role: Role, shops: frozenset[uuid.UUID] = frozenset()) -> AuthContext:
    return AuthContext(
        user_id=USER, assurance=AssuranceLevel.AAL2, session_id=None,
        memberships=(Membership(ORG, role, shops),), claims={"sub": str(USER)},
    )  # fmt: skip


def test_the_day_is_the_users_day() -> None:
    start, end = day_range(date(2026, 10, 27), "Asia/Jerusalem")
    assert (start, end) == (
        datetime(2026, 10, 26, 22, 0, tzinfo=UTC),
        datetime(2026, 10, 27, 22, 0, tzinfo=UTC),
    )
    # The day summer time ends there has 25 hours.
    start, end = day_range(date(2026, 10, 25), "Asia/Jerusalem")
    assert end - start == timedelta(hours=25)


@pytest.mark.ac("AC-7.8.1")
def test_ac_7_8_1_what_did_an_automation_do_today_in_the_users_zone_and_shops() -> None:
    start, end = day_range(date(2026, 10, 27), "Asia/Jerusalem")
    reader = MemoryActivity(
        [
            today := record(start + timedelta(hours=1)),
            record(start - timedelta(minutes=1)),  # yesterday there
            record(end),  # tomorrow there
            record(start + timedelta(hours=2), actor=SYNC),  # another actor
            record(start + timedelta(hours=3), shop=OTHER_SHOP),  # a shop not granted
            record(start + timedelta(hours=4), shop=None),  # organization-wide
        ]
    )

    page = list_activity(
        acting(Role.OPERATIONS, frozenset({SHOP})), reader, organization_id=None,
        where=ActivityFilter(actor=INTAKE, since=start, until=end), now=NOW,
    )  # fmt: skip

    assert page.items == (today,)
    assert page.source == ACTIVITY_SOURCE
    assert page.read_at == NOW


def test_owners_see_every_shop_and_organization_wide_records() -> None:
    reader = MemoryActivity(
        [record(NOW - timedelta(hours=h), shop=s) for h, s in ((1, SHOP), (2, OTHER_SHOP),
                                                                (3, None))]
    )  # fmt: skip

    page = list_activity(
        acting(Role.OWNER), reader, organization_id=ORG, where=ActivityFilter(), now=NOW
    )

    assert [r.shop_id for r in page.items] == [SHOP, OTHER_SHOP, None]


@pytest.mark.ac("AC-7.8.2")
def test_ac_7_8_2_which_automations_failed_this_week() -> None:
    week = NOW - timedelta(days=7)
    failed = [
        record(NOW - timedelta(days=d), outcome=ActivityOutcome.FAILED, actor=a)
        for d, a in ((1, INTAKE), (3, SYNC))
    ]
    reader = MemoryActivity(
        [
            *failed,
            record(NOW - timedelta(days=2)),  # succeeded
            record(NOW - timedelta(days=9), outcome=ActivityOutcome.FAILED),  # last week
            record(NOW - timedelta(days=1), outcome=ActivityOutcome.FAILED, actor=SHON,
                   kind=ActivityKind.TASK_EXECUTION),  # an employee, not an automation
        ]
    )  # fmt: skip

    page = list_activity(
        acting(Role.OWNER), reader, organization_id=ORG, now=NOW,
        where=ActivityFilter(actor_kind=ActorKind.AUTOMATION, outcome=ActivityOutcome.FAILED,
                             since=week, until=NOW),
    )  # fmt: skip

    assert page.items == tuple(failed)


def test_filters_by_kind_shop_and_task() -> None:
    wanted = record(NOW - timedelta(hours=1), kind=ActivityKind.INCIDENT, task=TASK)
    reader = MemoryActivity(
        [wanted, record(NOW - timedelta(hours=2), kind=ActivityKind.INCIDENT, shop=OTHER_SHOP),
         record(NOW - timedelta(hours=3), task=TASK)]
    )  # fmt: skip

    page = list_activity(
        acting(Role.ADMIN), reader, organization_id=ORG, now=NOW,
        where=ActivityFilter(kind=ActivityKind.INCIDENT, shop_id=SHOP, task_id=TASK),
    )  # fmt: skip

    assert page.items == (wanted,)


def test_pages_follow_the_cursor_without_gaps() -> None:
    rows = [record(NOW - timedelta(minutes=m)) for m in range(5)]
    reader = MemoryActivity(rows)
    seen: list[ActivityRecord] = []
    before = None
    while True:
        page = list_activity(
            acting(Role.OWNER), reader, organization_id=ORG, where=ActivityFilter(), now=NOW,
            limit=2, before=before,
        )  # fmt: skip
        seen.extend(page.items)
        if page.next_before is None:
            break
        before = page.next_before
    assert seen == rows


@pytest.mark.ac("AC-7.8.3")
def test_ac_7_8_3_a_tasks_trail_is_every_correlated_record_in_causal_order() -> None:
    reader = MemoryActivity(
        [
            third := record(NOW, actor=SYNC, correlation="c-7", causation="job:2"),
            first := record(NOW - timedelta(minutes=2), actor=SHON, correlation="c-7", task=TASK,
                            kind=ActivityKind.TASK_EXECUTION),
            second := record(NOW - timedelta(minutes=1), correlation="c-7", causation="job:1"),
            record(NOW - timedelta(minutes=1), correlation="c-8"),  # another trail
        ]
    )  # fmt: skip

    page = task_trail(acting(Role.OWNER), reader, TASK, organization_id=ORG, now=NOW)

    assert page.items == (first, second, third)
    assert {r.actor for r in page.items} == {SHON, INTAKE, SYNC}


@pytest.mark.ac("AC-7.8.4")
def test_ac_7_8_4_an_empty_result_is_no_recorded_activity_with_its_source() -> None:
    page = list_activity(
        acting(Role.OWNER), MemoryActivity(), organization_id=ORG, where=ActivityFilter(), now=NOW
    )

    assert page.items == ()
    assert page.empty_reason == "no_recorded_activity"
    assert page.source == "app.activity_records"
    assert page.read_at == NOW


def test_roles_without_the_capability_are_refused() -> None:
    with pytest.raises(PermissionError):
        list_activity(
            acting(Role.VIEWER), MemoryActivity(), organization_id=ORG,
            where=ActivityFilter(), now=NOW,
        )  # fmt: skip
    with pytest.raises(PermissionError):
        task_trail(acting(Role.VIEWER), MemoryActivity(), TASK, organization_id=ORG, now=NOW)


def test_a_range_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="range"):
        ActivityFilter(since=NOW, until=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="timezone"):
        ActivityFilter(since=datetime(2026, 10, 27))  # noqa: DTZ001 - the test
