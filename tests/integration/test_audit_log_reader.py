"""The audit view's reader (AC-3.9.1) against real Postgres and the local seed: it reads as the
caller, so the table's policy lets only the Owner and Admins see rows; the filters and the cursor
work in SQL. Each test writes its own records under a fresh action family (the log is
append-only)."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from htco_application.audit_log import AuditQuery
from htco_domain.actors import ActorKind
from htco_persistence.audit import PostgresAuditLogReader
from htco_persistence.database import Database
from tests.integration.test_shop_reader import DEMO, DEMO_OWNER, OTHER, SHOP_ONE, claims

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DEMO_ADMIN = uuid.UUID("c0000000-0000-0000-0000-000000000002")
DEMO_DEVELOPER = uuid.UUID("c0000000-0000-0000-0000-000000000006")
START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set; start the database with `just db-start`")
    db = Database(url, min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def write(database: Database, family: str, rows: list[dict[str, Any]]) -> None:
    with database.connection() as connection, connection.transaction():
        for n, row in enumerate(rows):
            connection.execute(
                """
                insert into audit.audit_log
                  (id, occurred_at, organization_id, actor_kind, actor_id, action, entity_type,
                   entity_id, before, after)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    uuid.uuid7(),
                    START + timedelta(minutes=n),
                    row.get("organization_id", DEMO),
                    row.get("actor_kind", "user"),
                    row.get("actor_id", str(DEMO_OWNER)),
                    f"{family}.{row.get('verb', 'changed')}",
                    row.get("entity_type", "membership"),
                    row.get("entity_id", str(uuid.uuid4())),
                    None,
                    Jsonb(row["after"]) if "after" in row else None,
                ],
            )


@pytest.fixture
def family() -> str:
    return f"test_{uuid.uuid4().hex[:12]}"


@pytest.mark.ac("AC-3.9.1")
@pytest.mark.parametrize("user", [DEMO_OWNER, DEMO_ADMIN])
async def test_owner_and_admin__their_organizations_records_newest_first(
    database: Database, family: str, user: uuid.UUID
) -> None:
    write(database, family, [{"verb": "first"}, {"verb": "second"}, {"organization_id": OTHER}])

    page = await PostgresAuditLogReader(database).list_audit(
        claims(user), AuditQuery(DEMO, action=f"{family}.*")
    )

    assert [e.action for e in page.items] == [f"{family}.second", f"{family}.first"]
    assert page.next_before is None


@pytest.mark.ac("AC-3.9.2")
async def test_rls__a_developer_reads_nothing_even_unfiltered(
    database: Database, family: str
) -> None:
    write(database, family, [{}])

    page = await PostgresAuditLogReader(database).list_audit(
        claims(DEMO_DEVELOPER), AuditQuery(DEMO, action=f"{family}.*")
    )

    assert page.items == ()


async def test_filters__shop_actor_action_and_time(database: Database, family: str) -> None:
    write(
        database,
        family,
        [
            {"verb": "connected", "entity_type": "shop", "entity_id": str(SHOP_ONE)},
            {"verb": "synced", "actor_kind": "automation", "actor_id": "etsy-order-intake",
             "after": {"shop_id": str(SHOP_ONE)}},
            {"verb": "changed"},
        ],
    )  # fmt: skip
    reader = PostgresAuditLogReader(database)
    owner = claims(DEMO_OWNER)

    by_shop = await reader.list_audit(
        owner, AuditQuery(DEMO, shop_id=SHOP_ONE, action=f"{family}.*")
    )
    by_actor = await reader.list_audit(
        owner, AuditQuery(DEMO, actor_kind=ActorKind.AUTOMATION, action=f"{family}.*")
    )
    exact = await reader.list_audit(owner, AuditQuery(DEMO, action=f"{family}.changed"))
    window = await reader.list_audit(
        owner,
        AuditQuery(
            DEMO, action=f"{family}.*", since=START + timedelta(minutes=1),
            until=START + timedelta(minutes=2),
        ),
    )  # fmt: skip

    assert [e.action for e in by_shop.items] == [f"{family}.synced", f"{family}.connected"]
    assert [e.actor_id for e in by_actor.items] == ["etsy-order-intake"]
    assert [e.action for e in exact.items] == [f"{family}.changed"]
    assert [e.action for e in window.items] == [f"{family}.synced"]


async def test_cursor__pages_through_without_overlap(database: Database, family: str) -> None:
    write(database, family, [{} for _ in range(5)])
    reader = PostgresAuditLogReader(database)

    first = await reader.list_audit(
        claims(DEMO_OWNER), AuditQuery(DEMO, action=f"{family}.*", limit=3)
    )
    second = await reader.list_audit(
        claims(DEMO_OWNER),
        AuditQuery(DEMO, action=f"{family}.*", limit=3, before=first.next_before),
    )

    assert len(first.items) == 3
    assert first.next_before == first.items[-1].id
    assert len(second.items) == 2
    assert second.next_before is None
    assert not {e.id for e in first.items} & {e.id for e in second.items}
