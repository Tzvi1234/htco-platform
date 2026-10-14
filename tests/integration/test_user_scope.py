"""User-scoped transactions and the membership reader against real Postgres (slice 2.3).

Uses the local seed (`supabase/seed.sql`): Demo Workshop with one user per role, where Support is
granted "Demo Shop One" only, and Other Studio with its owner. Needs `HTCO_TEST_DATABASE_URL`;
run with `just test-db` after `just db-reset`. CI runs it in the `database` job.
"""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_domain.identity import AssuranceLevel, Role
from htco_persistence.database import Database
from htco_persistence.identity import PostgresMembershipReader

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DEMO = uuid.UUID("c1000000-0000-0000-0000-000000000000")
OTHER = uuid.UUID("d1000000-0000-0000-0000-000000000000")
DEMO_OWNER = uuid.UUID("c0000000-0000-0000-0000-000000000001")
DEMO_OPERATIONS = uuid.UUID("c0000000-0000-0000-0000-000000000003")
DEMO_SUPPORT = uuid.UUID("c0000000-0000-0000-0000-000000000005")
OTHER_OWNER = uuid.UUID("d0000000-0000-0000-0000-000000000001")
SHOP_ONE = uuid.UUID("c3000000-0000-0000-0000-000000000001")
SHOP_TWO = uuid.UUID("c3000000-0000-0000-0000-000000000002")
OTHER_SHOP = uuid.UUID("d3000000-0000-0000-0000-000000000001")


def claims(user_id: uuid.UUID, aal: str = "aal1") -> dict[str, Any]:
    return {"sub": str(user_id), "role": "authenticated", "aal": aal}


def user(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, assurance=AssuranceLevel.AAL1, session_id=None, claims=claims(user_id)
    )


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


def shop_ids(db: Database, token_claims: dict[str, Any]) -> set[uuid.UUID]:
    # Deliberately unfiltered: only RLS decides what comes back.
    with db.user_transaction(token_claims) as connection:
        return {row[0] for row in connection.execute("select id from app.shops").fetchall()}


async def test_unfiltered_query__returns_only_the_users_tenant_rows(database: Database) -> None:
    """AC-2.3.3."""
    assert shop_ids(database, claims(DEMO_SUPPORT)) == {SHOP_ONE}
    assert shop_ids(database, claims(DEMO_OWNER)) == {SHOP_ONE, SHOP_TWO}
    assert shop_ids(database, claims(OTHER_OWNER)) == {OTHER_SHOP}


async def test_user_transaction__without_a_subject__sees_nothing(database: Database) -> None:
    assert shop_ids(database, {"role": "authenticated"}) == set()


async def test_user_transaction__does_not_leak_the_role_to_the_next_user_of_the_connection(
    database: Database,
) -> None:
    with database.user_transaction(claims(DEMO_SUPPORT)) as connection:
        inside = connection.execute("select current_user").fetchone()
    with database.connection() as connection:
        after = connection.execute(
            "select current_user, current_setting('request.jwt.claims', true)"
        ).fetchone()

    assert inside == ("authenticated",)
    assert after is not None
    assert after[0] != "authenticated"
    assert after[1] in (None, "")


async def test_membership_reader__support__one_organization_one_shop(database: Database) -> None:
    found = await PostgresMembershipReader(database).memberships_of(user(DEMO_SUPPORT))
    assert list(found) == [Membership(DEMO, Role.SUPPORT, frozenset({SHOP_ONE}))]


async def test_membership_reader__operations__both_granted_shops(database: Database) -> None:
    found = await PostgresMembershipReader(database).memberships_of(user(DEMO_OPERATIONS))
    assert list(found) == [Membership(DEMO, Role.OPERATIONS, frozenset({SHOP_ONE, SHOP_TWO}))]


async def test_membership_reader__owner__role_without_explicit_grants(database: Database) -> None:
    found = await PostgresMembershipReader(database).memberships_of(user(OTHER_OWNER))
    assert list(found) == [Membership(OTHER, Role.OWNER, frozenset())]


async def test_membership_reader__unknown_user__has_no_memberships(database: Database) -> None:
    found = await PostgresMembershipReader(database).memberships_of(user(uuid.uuid4()))
    assert list(found) == []
