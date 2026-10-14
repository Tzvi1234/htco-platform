"""The owner bootstrap against real Postgres (slice 2.5, AC-2.5.1 to AC-2.5.3)."""

import os
import uuid
from collections.abc import AsyncIterator

import anyio
import pytest

from htco_application.bootstrap import BootstrapRequest, BootstrapResult, bootstrap_owner
from htco_persistence.bootstrap import PostgresBootstrapStore
from htco_persistence.database import Database

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set; start the database with `just db-start`")
    db = Database(url, min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def new_user(database: Database) -> str:
    """A signed-up user with a unique email (tests share the database and never clean up)."""
    email = f"owner-{uuid.uuid4().hex[:12]}@example.test"
    with database.connection() as connection:
        connection.execute(
            "insert into auth.users (id, email) values (%s, %s)", [uuid.uuid4(), email]
        )
    return email


def request(email: str) -> BootstrapRequest:
    return BootstrapRequest(email=email.upper(), organization_name="HT Commerce", display_name="T")


def count(database: Database, sql: str, *params: object) -> int:
    with database.connection() as connection:
        row = connection.execute(sql, list(params)).fetchone()
    assert row is not None
    return int(row[0])


async def test_bootstrap_twice__one_organization_one_membership(database: Database) -> None:
    """AC-2.5.1."""
    email = new_user(database)
    store = PostgresBootstrapStore(database)

    first = await bootstrap_owner(store, request(email), correlation_id="c1")
    second = await bootstrap_owner(store, request(email), correlation_id="c2")

    assert first.created
    assert not second.created
    assert second.organization_id == first.organization_id
    org = first.organization_id
    assert count(database, "select count(*) from app.organizations where id = %s", org) == 1
    assert (
        count(database, "select count(*) from app.memberships where organization_id = %s", org) == 1
    )
    assert (
        count(
            database,
            "select count(*) from app.memberships where organization_id = %s and role = 'owner'",
            org,
        )
        == 1
    )


async def test_bootstrap__audit_records__system_actor_entity_after_and_correlation(
    database: Database,
) -> None:
    """AC-2.5.2 and AC-2.5.3."""
    email = new_user(database)
    result = await bootstrap_owner(
        PostgresBootstrapStore(database), request(email), correlation_id="corr-xyz"
    )

    with database.connection() as connection:
        rows = connection.execute(
            """
            select actor_kind, actor_id, action, entity_type, entity_id, before, after,
                   correlation_id
            from audit.audit_log where organization_id = %s order by action
            """,
            [result.organization_id],
        ).fetchall()

    assert [row[:5] for row in rows] == [
        ("system", "bootstrap", "membership.created", "membership", str(result.membership_id)),
        (
            "system",
            "bootstrap",
            "organization.created",
            "organization",
            str(result.organization_id),
        ),
    ]
    assert all(row[5] is None for row in rows)
    assert rows[0][6]["role"] == "owner"
    assert rows[1][6] == {"name": "HT Commerce"}
    assert {row[7] for row in rows} == {"corr-xyz"}


async def test_bootstrap__started_twice_at_once__still_one_organization(
    database: Database,
) -> None:
    email = new_user(database)
    store = PostgresBootstrapStore(database)
    results: list[BootstrapResult] = []

    async def run() -> None:
        results.append(await bootstrap_owner(store, request(email), correlation_id="race"))

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        group.start_soon(run)

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.organization_id for result in results}) == 1
