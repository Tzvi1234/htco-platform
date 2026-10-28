"""The worker's database login against real Postgres (architecture audit 5.1, ER-SEC-08):
`Database.session_roles()`, the worker's startup check, and the `htco_worker_login` role."""

import uuid
from collections.abc import AsyncIterator

import anyio
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from htco_persistence.database import Database
from htco_worker.main import WORKER_LOGIN, WORKER_ROLE, DatabaseLoginError, check_database_login
from tests.integration.test_etsy_connection import _url

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


async def _roles(database: Database) -> tuple[str, str, bool]:
    await database.start()
    try:
        return await anyio.to_thread.run_sync(database.session_roles)
    finally:
        await database.stop()


async def test_session_roles__the_api_login__is_its_own_role(admin: Database) -> None:
    session_user, current_user, _ = await anyio.to_thread.run_sync(admin.session_roles)

    assert session_user == current_user == conninfo_to_dict(_url())["user"]


async def test_session_roles__the_fallback__switches_role_and_still_warns_only() -> None:
    """The API's login switched to the worker role: allowed as a fallback, refused once the
    worker's own login is configured (the API's credential behind the worker's settings)."""
    roles = await _roles(Database(_url(), min_size=1, max_size=1, role=WORKER_ROLE))

    assert roles[0] == conninfo_to_dict(_url())["user"]
    assert roles[1] == WORKER_ROLE
    check_database_login(roles, own_login=False)
    with pytest.raises(DatabaseLoginError):
        check_database_login(roles, own_login=True)


async def test_session_roles__the_worker_login__passes_the_check(admin: Database) -> None:
    """The migration's `htco_worker_login`, with a password set for this test only (the owner
    sets the real one in Supabase): unprivileged, it runs as the worker role."""
    password = f"fake-{uuid.uuid4().hex}"
    statement = sql.SQL("alter role {} password {}")
    with admin.connection() as connection:
        connection.execute(statement.format(sql.Identifier(WORKER_LOGIN), sql.Literal(password)))
        connection.commit()
    try:
        url = make_conninfo(_url(), user=WORKER_LOGIN, password="")
        roles = await _roles(
            Database(url, min_size=1, max_size=1, role=WORKER_ROLE, password=password)
        )
    finally:
        with admin.connection() as connection:
            connection.execute(statement.format(sql.Identifier(WORKER_LOGIN), sql.NULL))
            connection.commit()

    assert roles == (WORKER_LOGIN, WORKER_ROLE, False)
    check_database_login(roles, own_login=True)
