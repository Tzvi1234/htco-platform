"""Access management against real Postgres (slice 2.6, AC-2.6.3 and RLS as a second line).

Runs as the seed's Demo Workshop owner on newly created users, so the seed itself stays as it is.
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from htco_application.access.authentication import (
    AuthContext,
    AuthenticatedUser,
    Membership,
    NoMembershipError,
    authenticate,
)
from htco_application.access.management import (
    AccessDeniedError,
    InviteeAlreadyRegisteredError,
    change_role,
    grant_shop,
    invite_member,
    list_members,
    remove_member,
    revoke_shop,
)
from htco_domain.identity import AssuranceLevel, Role
from htco_persistence.access import PostgresAccessStore
from htco_persistence.database import Database
from htco_persistence.identity import PostgresMembershipReader

pytestmark = [pytest.mark.db, pytest.mark.anyio]

DEMO = uuid.UUID("c1000000-0000-0000-0000-000000000000")
DEMO_OWNER = uuid.UUID("c0000000-0000-0000-0000-000000000001")
SHOP_ONE = uuid.UUID("c3000000-0000-0000-0000-000000000001")
NOW = datetime.now(UTC)


def claims(user_id: uuid.UUID, aal: str) -> dict[str, Any]:
    return {"sub": str(user_id), "role": "authenticated", "aal": aal}


OWNER_AAL2 = AuthContext(
    user_id=DEMO_OWNER,
    assurance=AssuranceLevel.AAL2,
    session_id=None,
    memberships=(Membership(DEMO, Role.OWNER, frozenset()),),
    claims=claims(DEMO_OWNER, "aal2"),
)


class FixedInviter:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id

    async def invite(self, email: str) -> uuid.UUID:
        assert "@" in email
        return self.user_id


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


def signed_up_user(database: Database) -> uuid.UUID:
    user_id = uuid.uuid4()
    with database.connection() as connection:
        connection.execute(
            "insert into auth.users (id, email) values (%s, %s)",
            [user_id, f"member-{user_id.hex[:12]}@example.test"],
        )
    return user_id


async def test_invite_change_grant_revoke_remove__audited_and_effective_at_once(
    database: Database,
) -> None:
    store = PostgresAccessStore(database)
    newcomer = signed_up_user(database)
    reader = PostgresMembershipReader(database)
    as_newcomer = AuthenticatedUser(
        user_id=newcomer,
        assurance=AssuranceLevel.AAL1,
        session_id=None,
        claims=claims(newcomer, "aal1"),
    )

    member = await invite_member(
        OWNER_AAL2,
        store,
        FixedInviter(newcomer),
        DEMO,
        email="new@example.test",
        role=Role.VIEWER,
        now=NOW,
        correlation_id="it-1",
    )
    await change_role(
        OWNER_AAL2, store, DEMO, member.id, Role.SUPPORT, now=NOW, correlation_id="it-2"
    )
    await grant_shop(OWNER_AAL2, store, DEMO, member.id, SHOP_ONE, now=NOW, correlation_id="it-3")

    context = await authenticate(as_newcomer, reader)
    assert context.principal_for(DEMO).role is Role.SUPPORT
    assert context.principal_for(DEMO).granted_shops == frozenset({SHOP_ONE})

    await revoke_shop(OWNER_AAL2, store, DEMO, member.id, SHOP_ONE, now=NOW, correlation_id="it-4")
    assert (await authenticate(as_newcomer, reader)).principal_for(
        DEMO
    ).granted_shops == frozenset()

    await remove_member(OWNER_AAL2, store, DEMO, member.id, now=NOW, correlation_id="it-5")
    # AC-2.6.3: the next request with the same, still valid token finds no membership.
    with pytest.raises(NoMembershipError):
        await authenticate(as_newcomer, reader)

    with database.connection() as connection:
        rows = connection.execute(
            """
            select action, actor_kind, actor_id, correlation_id from audit.audit_log
            where correlation_id like 'it-%%' and (entity_id = %s or after ->> 'membership_id' = %s
                  or before ->> 'membership_id' = %s)
            order by occurred_at, correlation_id
            """,
            [str(member.id), str(member.id), str(member.id)],
        ).fetchall()
    assert [row[0] for row in rows] == [
        "membership.created",
        "membership.role_changed",
        "shop_access.granted",
        "shop_access.revoked",
        "membership.revoked",
    ]
    assert {(row[1], row[2]) for row in rows} == {("user", str(DEMO_OWNER))}


async def test_list_members__names_roles_and_grants_from_the_database(database: Database) -> None:
    members = await list_members(OWNER_AAL2, PostgresAccessStore(database), DEMO, now=NOW)
    by_role = {member.role: member for member in members}

    assert by_role[Role.OWNER].display_name == "Demo Owner"
    assert by_role[Role.SUPPORT].shop_ids == frozenset({SHOP_ONE})
    assert {member.organization_id for member in members} == {DEMO}


async def test_rls__rejects_membership_writes_from_an_aal1_session_even_past_the_app(
    database: Database,
) -> None:
    """The database refuses the write on its own: defense in depth behind the policy engine."""
    newcomer = signed_up_user(database)
    store = PostgresAccessStore(database)

    def create(uow: Any) -> None:
        uow.create_membership(uuid.uuid4(), DEMO, newcomer, Role.VIEWER)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await store.run(claims(DEMO_OWNER, "aal1"), create)


# Review fixes ------------------------------------------------------------------------------------


class RegisteredInviter:
    """The sign-in service already knows the email."""

    async def invite(self, email: str) -> uuid.UUID:
        raise InviteeAlreadyRegisteredError(email)


async def test_invite__an_already_registered_email__attaches_that_user(database: Database) -> None:
    store = PostgresAccessStore(database)
    known = signed_up_user(database)
    with database.connection() as connection:
        row = connection.execute("select email from auth.users where id = %s", [known]).fetchone()
    assert row is not None

    member = await invite_member(
        OWNER_AAL2,
        store,
        RegisteredInviter(),
        DEMO,
        email=str(row[0]).upper(),
        role=Role.VIEWER,
        now=NOW,
        correlation_id="it-registered",
    )
    assert member.user_id == known


async def test_admin__removes_their_own_membership(database: Database) -> None:
    store = PostgresAccessStore(database)
    admin = signed_up_user(database)
    member = await invite_member(
        OWNER_AAL2,
        store,
        FixedInviter(admin),
        DEMO,
        email="a@example.test",
        role=Role.ADMIN,
        now=NOW,
        correlation_id="it-self-1",
    )
    as_admin = AuthContext(
        user_id=admin,
        assurance=AssuranceLevel.AAL2,
        session_id=None,
        memberships=(Membership(DEMO, Role.ADMIN, frozenset()),),
        claims=claims(admin, "aal2"),
    )

    await remove_member(as_admin, store, DEMO, member.id, now=NOW, correlation_id="it-self-2")

    with database.connection() as connection:
        row = connection.execute(
            "select revoked_at is not null from app.memberships where id = %s", [member.id]
        ).fetchone()
    assert row == (True,)


async def test_owner_rows__refuse_role_changes_and_revocation_in_the_database(
    database: Database,
) -> None:
    store = PostgresAccessStore(database)
    owner_membership = uuid.UUID("c2000000-0000-0000-0000-000000000001")

    def demote(uow: Any) -> None:
        uow.update_role(owner_membership, Role.VIEWER)

    def revoke(uow: Any) -> None:
        uow.revoke_membership(owner_membership, NOW)

    for work in (demote, revoke):
        with pytest.raises(AccessDeniedError):
            await store.run(claims(DEMO_OWNER, "aal2"), work)


async def test_create_grant__twice__second_is_a_no_op(database: Database) -> None:
    store = PostgresAccessStore(database)
    newcomer = signed_up_user(database)
    member = await invite_member(
        OWNER_AAL2,
        store,
        FixedInviter(newcomer),
        DEMO,
        email="g@example.test",
        role=Role.SUPPORT,
        now=NOW,
        correlation_id="it-grant",
    )

    def grant_twice(uow: Any) -> tuple[bool, bool]:
        first = uow.create_grant(uuid.uuid4(), DEMO, member.id, SHOP_ONE)
        second = uow.create_grant(uuid.uuid4(), DEMO, member.id, SHOP_ONE)
        return first, second

    assert await store.run(claims(DEMO_OWNER, "aal2"), grant_twice) == (True, False)
