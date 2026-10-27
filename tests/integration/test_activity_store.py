"""Activity records on real Postgres (slice 7.7): an entry and its state change commit together or
not at all, the row keeps every field and its template, it lands in its month's partition, the
application roles cannot change or delete it (AC-7.7.2), and the worker writes its job's
organization only."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import psycopg
import pytest

from htco_application.activity import ActivityEntry, new_activity, summarize
from htco_domain.activity import ActivityKind, ActivityOutcome
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.activity import PostgresActivityLog, append_activity
from htco_persistence.database import Database, tenant_scope
from tests.integration.test_etsy_connection import _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
SYNC = ActorRef(ActorKind.AUTOMATION, "tracking-sync")


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def organization(admin: Database) -> tuple[uuid.UUID, uuid.UUID]:
    org, shop = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.organizations (id, name) values (%s, %s)", [org, f"Activity {org}"]
        )
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Shop', 'etsy', 'active')",
            [shop, org],
        )
    return org, shop


def an_entry(org: uuid.UUID, shop: uuid.UUID | None, correlation_id: str) -> ActivityEntry:
    return new_activity(
        kind=ActivityKind.INCIDENT,
        organization_id=org,
        shop_id=shop,
        actor=SYNC,
        action="incidents.opened",
        outcome=ActivityOutcome.SUCCEEDED,
        summary=summarize("incident.opened", incident="inc-7", receipt="4471"),
        occurred_at=NOW,
        correlation_id=correlation_id,
        causation_id="evt-1",
        task_id=uuid.UUID(int=9),
    )


async def test_append__keeps_every_field_and_lands_in_its_month(admin: Database) -> None:
    org, shop = organization(admin)
    entry = an_entry(org, shop, "c-1")

    await PostgresActivityLog(admin).append(entry)

    with admin.connection() as connection:
        row = connection.execute(
            "select tableoid::regclass::text, organization_id, shop_id, kind, actor_kind, actor_id,"
            " action, outcome, summary_template, summary, occurred_at, correlation_id,"
            " causation_id, task_id from app.activity_records where id = %s",
            [entry.record.id],
        ).fetchone()
    assert row == (
        "app.activity_records_2026_11", org, shop, "incident", "automation", "tracking-sync",
        "incidents.opened", "succeeded", "incident.opened",
        "Opened incident inc-7 for receipt 4471", NOW, "c-1", "evt-1", uuid.UUID(int=9),
    )  # fmt: skip


async def test_append__rolls_back_with_the_change_it_describes(admin: Database) -> None:
    org, _ = organization(admin)

    def change_that_fails() -> None:
        with admin.connection() as connection, connection.transaction():
            append_activity(connection, an_entry(org, None, "c-rollback"))
            raise RuntimeError("the state change failed")

    with pytest.raises(RuntimeError):
        change_that_fails()

    count = scalar(
        admin, "select count(*) from app.activity_records where organization_id = %s", [org]
    )
    assert count == 0


async def test_append__refuses_anything_but_an_entry(admin: Database) -> None:
    with pytest.raises(TypeError, match="ActivityEntry"), admin.connection() as connection:
        append_activity(connection, {"summary": "free text"})  # type: ignore[arg-type]


@pytest.mark.ac("AC-7.7.2")
@pytest.mark.parametrize("role", [None, "htco_worker"])
async def test_records__cannot_be_changed_or_deleted(admin: Database, role: str | None) -> None:
    org, shop = organization(admin)
    entry = an_entry(org, shop, "c-immutable")
    await PostgresActivityLog(admin).append(entry)
    database = Database(_url(), min_size=1, max_size=1, role=role)
    await database.start()
    try:
        for statement in (
            "update app.activity_records set summary = 'x' where id = %s",
            "delete from app.activity_records where id = %s",
        ):
            with (
                tenant_scope(org),
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                database.connection() as connection,
            ):
                connection.execute(statement, [entry.record.id])
    finally:
        await database.stop()


async def test_worker__appends_its_jobs_organization_only(
    admin: Database, worker_db: Database
) -> None:
    org, shop = organization(admin)
    other, _ = organization(admin)
    log = PostgresActivityLog(worker_db)

    with tenant_scope(org):
        await log.append(an_entry(org, shop, "w-1"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await log.append(an_entry(other, None, "w-2"))

    assert scalar(
        admin, "select count(*) from app.activity_records where correlation_id like 'w-%%'"
        " and organization_id in (%s, %s)", [org, other],
    ) == 1  # fmt: skip


async def test_removing_the_organization__removes_its_records(admin: Database) -> None:
    org, shop = organization(admin)
    await PostgresActivityLog(admin).append(an_entry(org, shop, "c-gone"))

    with admin.connection() as connection:
        connection.execute("delete from app.organizations where id = %s", [org])

    count = scalar(
        admin, "select count(*) from app.activity_records where organization_id = %s", [org]
    )
    assert count == 0
