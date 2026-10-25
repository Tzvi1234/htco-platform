"""The Browser Controller against real Postgres (slice 8A.2): the advisory profile lock (one
session per profile, parallel shops, released when the holder's connection dies) and the
session log with the environment's error counter."""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import psycopg
import pytest

from htco_application.browser_controller import BrowserController, SessionTimeoutError
from htco_application.browser_profiles import ProfileVault, ProfileWorkspaces
from htco_persistence.browser_profiles import PostgresBrowserProfiles
from htco_persistence.browser_sessions import PostgresProfileLocks, PostgresSessionLog, lock_key
from htco_persistence.database import Database, tenant_scope
from htco_platform.crypto import EnvelopeSealer, KeyRing
from tests.integration.test_browser_profile_store import an_environment
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def browser() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_browser")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def soon(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.mark.ac("AC-8A.2.1")
async def test_lock__one_holder_per_environment_and_parallel_environments(
    admin: Database,
) -> None:
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    one, two = uuid.uuid4(), uuid.uuid4()
    async with locks.hold(one, until=soon(1)) as first:
        async with locks.hold(one, until=soon(0.3)) as second:
            assert (first, second) == (True, False)
        async with locks.hold(two, until=soon(0.3)) as other:
            assert other
    async with locks.hold(one, until=soon(0.3)) as again:
        assert again


@pytest.mark.ac("AC-8A.2.2")
async def test_lock__a_killed_holder_releases_the_profile(admin: Database) -> None:
    environment = uuid.uuid4()
    holder = psycopg.connect(_url(), autocommit=True)
    try:
        holder.execute(
            "select pg_try_advisory_lock(hashtextextended(%s, 0))", [lock_key(environment)]
        )
        pid = holder.info.backend_pid
        locks = PostgresProfileLocks(admin, poll_seconds=0.05)
        async with locks.hold(environment, until=soon(0.2)) as held:
            assert not held
        scalar(admin, "select pg_terminate_backend(%s)", [pid])  # the worker dies
        async with locks.hold(environment, until=soon(2)) as held:
            assert held
    finally:
        holder.close()


@dataclass
class Handle:
    async def close(self) -> None:
        return None


class Engine:
    async def launch(self, profile_directory: Path) -> Handle:  # noqa: ARG002
        return Handle()


@pytest.mark.ac("AC-8A.2.3")
async def test_session__a_timeout_is_recorded_and_counted(
    admin: Database, browser: Database, tmp_path: Path
) -> None:
    tenant = Tenant(admin)
    _, environment_id = an_environment(admin, tenant)
    profiles = PostgresBrowserProfiles(browser)
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), profiles, lambda: datetime.now(UTC)
    )
    controller = BrowserController(
        profiles, ProfileWorkspaces(vault, tmp_path), Engine(),
        PostgresProfileLocks(admin, poll_seconds=0.05), PostgresSessionLog(browser),
        clock=lambda: datetime.now(UTC),
    )  # fmt: skip

    with tenant_scope(tenant.organization_id):
        async with controller.session(environment_id, purpose="test.ok", deadline=soon(5)):
            pass
        with pytest.raises(SessionTimeoutError):
            async with controller.session(environment_id, purpose="test.slow", deadline=soon(1)):
                await anyio.sleep(5)

    with admin.connection() as connection:
        rows = connection.execute(
            "select purpose, outcome, failure, ended_at is not null from app.browser_sessions "
            "where environment_id = %s order by started_at",
            [environment_id],
        ).fetchall()
    assert rows == [("test.ok", "succeeded", None, True), ("test.slow", "failed", "timeout", True)]
    assert scalar(
        admin, "select error_count from app.shop_browser_environments where id = %s",
        [environment_id],
    ) == 1  # fmt: skip
