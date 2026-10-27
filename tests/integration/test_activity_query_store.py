"""The activity query on real Postgres (slice 7.8): filters and the shop scope before the limit,
keyset pages, and a task's trail by correlation in recorded order."""

import uuid
from datetime import timedelta

import pytest

from htco_application.activity import new_activity, summarize
from htco_application.activity_query import ActivityCursor, ActivityFilter
from htco_domain.activity import ActivityKind, ActivityOutcome
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.activity import PostgresActivityLog, PostgresActivityReader
from htco_persistence.database import Database
from tests.integration.test_activity_store import NOW, SYNC, admin, organization

pytestmark = [pytest.mark.db, pytest.mark.anyio]
__all__ = ["admin"]  # the fixture

INTAKE = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")


async def append(  # noqa: PLR0913 - one argument per varied field
    db: Database,
    org: uuid.UUID,
    shop: uuid.UUID | None,
    *,
    minutes: int,
    actor: ActorRef = SYNC,
    outcome: ActivityOutcome = ActivityOutcome.SUCCEEDED,
    correlation: str = "c-1",
    task: uuid.UUID | None = None,
) -> uuid.UUID:
    entry = new_activity(
        kind=ActivityKind.AUTOMATION_EXECUTION, organization_id=org, shop_id=shop, actor=actor,
        action="tracking.recorded", outcome=outcome,
        summary=summarize("incident.opened", incident="inc-7", receipt="4471"),
        occurred_at=NOW + timedelta(minutes=minutes), correlation_id=correlation,
        causation_id=None, task_id=task,
    )  # fmt: skip
    await PostgresActivityLog(db).append(entry)
    return entry.record.id


async def test_filters_and_shop_scope_apply_before_the_limit(admin: Database) -> None:
    org, shop = organization(admin)
    stranger, stranger_shop = organization(admin)
    wanted = await append(admin, org, shop, minutes=1, outcome=ActivityOutcome.FAILED)
    await append(admin, org, None, minutes=2, outcome=ActivityOutcome.FAILED)  # org-wide
    await append(admin, org, shop, minutes=3, outcome=ActivityOutcome.FAILED, actor=INTAKE)
    await append(admin, org, shop, minutes=4)  # succeeded
    await append(admin, stranger, stranger_shop, minutes=1, outcome=ActivityOutcome.FAILED)
    reader = PostgresActivityReader(admin)

    rows = reader.list_activity(
        org,
        where=ActivityFilter(
            actor=SYNC, outcome=ActivityOutcome.FAILED,
            since=NOW, until=NOW + timedelta(hours=1),
        ),
        shop_ids=frozenset({shop}),
        limit=1,
    )  # fmt: skip

    assert [r.id for r in rows] == [wanted]
    everything = reader.list_activity(org, where=ActivityFilter(), shop_ids=None, limit=10)
    assert len(everything) == 4  # an Owner sees the organization-wide record too
    assert [r.occurred_at for r in everything] == sorted(
        (r.occurred_at for r in everything), reverse=True
    )


async def test_keyset_pages(admin: Database) -> None:
    org, shop = organization(admin)
    ids = [await append(admin, org, shop, minutes=m) for m in range(3)]
    reader = PostgresActivityReader(admin)

    [newest] = reader.list_activity(org, where=ActivityFilter(), shop_ids=None, limit=1)
    rest = reader.list_activity(
        org, where=ActivityFilter(), shop_ids=None, limit=10,
        before=ActivityCursor(newest.occurred_at, newest.id),
    )  # fmt: skip

    assert [newest.id, *(r.id for r in rest)] == list(reversed(ids))


async def test_a_tasks_trail_follows_its_correlation_ids_in_order(admin: Database) -> None:
    org, shop = organization(admin)
    task = uuid.uuid4()
    first = await append(admin, org, shop, minutes=1, correlation="c-t", task=task)
    second = await append(admin, org, shop, minutes=2, correlation="c-t", actor=INTAKE)
    await append(admin, org, shop, minutes=3, correlation="c-other")

    rows = PostgresActivityReader(admin).task_trail(org, task, shop_ids=None, limit=50)

    assert [r.id for r in rows] == [first, second]
