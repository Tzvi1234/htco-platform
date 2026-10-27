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
from htco_application.browser_profiles import (
    ProfileFenceLostError,
    ProfileRecordError,
    ProfileVault,
    ProfileWorkspaces,
)
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
            assert (first.held, second.held) == (True, False)
        async with locks.hold(two, until=soon(0.3)) as other:
            assert other.held
    async with locks.hold(one, until=soon(0.3)) as again:
        assert again.held


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
        async with locks.hold(environment, until=soon(0.2)) as waiting:
            assert not waiting.held
        scalar(admin, "select pg_terminate_backend(%s)", [pid])  # the worker dies
        async with locks.hold(environment, until=soon(2)) as lease:
            assert lease.held
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


@pytest.mark.ac("AC-8A.2.2")
async def test_lock__a_dropped_lock_connection_is_noticed(admin: Database) -> None:
    """quill on #121: before writing the profile back, the controller asks; a holder whose
    connection was killed no longer holds the lock."""
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    environment = uuid.uuid4()
    async with locks.hold(environment, until=soon(1)) as lease:
        assert lease.held
        assert await locks.still_held(lease)
        pid = scalar(
            admin,
            "select pid from pg_locks where locktype = 'advisory' and granted "
            "and objid = (hashtextextended(%s, 0) & 4294967295)::oid",
            [lock_key(environment)],
        )
        scalar(admin, "select pg_terminate_backend(%s)", [pid])
        assert not await locks.still_held(lease)
    assert not await locks.still_held(lease)


@pytest.mark.ac("AC-8A.2.1")
async def test_lock__a_deferred_waiter_leaves_the_holder_tracked(admin: Database) -> None:
    """Codex on #121, on real Postgres: the waiter that gives up does not make the holder's
    `still_held` false while the holder's connection still owns the lock."""
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    environment = uuid.uuid4()
    async with locks.hold(environment, until=soon(2)) as first:
        assert first.held
        async with locks.hold(environment, until=soon(0.2)) as second:
            assert not second.held
        assert await locks.still_held(first)


@pytest.mark.ac("AC-8A.2.2")
async def test_lock__a_dropped_lease_stays_lost_after_another_session_takes_the_lock(
    admin: Database,
) -> None:
    """sage and Codex on #121/#122, on real Postgres: S1's backend is killed, S2 takes the
    lock on a new connection, and S1's lease reports lost while S2's reports held."""
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    environment = uuid.uuid4()
    async with locks.hold(environment, until=soon(2)) as first:
        assert first.held
        pid = scalar(
            admin,
            "select pid from pg_locks where locktype = 'advisory' and granted "
            "and objid = (hashtextextended(%s, 0) & 4294967295)::oid",
            [lock_key(environment)],
        )
        scalar(admin, "select pg_terminate_backend(%s)", [pid])  # S1's connection drops
        async with locks.hold(environment, until=soon(2)) as second:
            assert second.held
            assert not await locks.still_held(first)
            assert await locks.still_held(second)


def _vault(profiles: PostgresBrowserProfiles) -> ProfileVault:
    return ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), profiles, lambda: datetime.now(UTC)
    )


@pytest.mark.ac("AC-8A.2.2")
async def test_profile__a_lost_lease_never_overwrites_a_newer_archive(
    admin: Database, browser: Database
) -> None:
    """Codex on #122 (#169), on real Postgres: S1 passes its fence check, then its lock's
    connection drops while it packs. S2 takes the lock and commits newer cookies. S1's write
    is then refused inside the write itself, so the stale cookies never win."""
    tenant = Tenant(admin)
    _, environment_id = an_environment(admin, tenant)
    profiles = PostgresBrowserProfiles(browser)
    vault = _vault(profiles)
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    with tenant_scope(tenant.organization_id):
        environment = profiles.by_id(environment_id)
        assert environment is not None
        async with locks.hold(environment_id, until=soon(3)) as first:
            assert await locks.still_held(first)  # S1's check passes
            scalar(
                admin, "select pg_terminate_backend(%s)", [first.connection.info.backend_pid]
            )  # S1's lock connection drops while it packs
            async with locks.hold(environment_id, until=soon(3)) as second:
                assert second.held
                vault.seal(environment, b"fresh", fence=second)
            with pytest.raises(ProfileFenceLostError):
                vault.seal(environment, b"stale", fence=first)
        assert vault.open(environment) == b"fresh"


@pytest.mark.ac("AC-8A.2.2")
async def test_profile__a_fenced_write_needs_this_backend_to_hold_the_lock(
    admin: Database, browser: Database
) -> None:
    """A lease that never took the lock, or another environment's lease, writes nothing; a lease
    that holds it writes, on its own connection, as the browser role in the tenant's scope."""
    tenant, other = Tenant(admin), Tenant(admin)
    _, environment_id = an_environment(admin, tenant)
    _, elsewhere_id = an_environment(admin, tenant)
    profiles = PostgresBrowserProfiles(browser)
    vault = _vault(profiles)
    locks = PostgresProfileLocks(admin, poll_seconds=0.05)
    with tenant_scope(tenant.organization_id):
        environment = profiles.by_id(environment_id)
        assert environment is not None
        async with locks.hold(elsewhere_id, until=soon(2)) as foreign:
            with pytest.raises(ProfileFenceLostError):
                vault.seal(environment, b"foreign", fence=foreign)
        async with locks.hold(environment_id, until=soon(2)) as lease:
            async with locks.hold(environment_id, until=soon(0.2)) as waiting:
                assert not waiting.held
                with pytest.raises(ProfileFenceLostError):
                    vault.seal(environment, b"waiting", fence=waiting)
            vault.seal(environment, b"held", fence=lease)
            # The transaction's role and tenant were `set local`: the lock's connection is back
            # to its login role with no tenant.
            row = lease.connection.execute(
                "select current_user, current_setting('htco.organization_id', true)"
            ).fetchone()
            assert row is not None
            assert row[0] != "htco_browser"
            assert row[1] in (None, "")
            # The backend is alive but no longer holds the lock: the check inside the write
            # refuses it.
            lease.connection.execute(
                "select pg_advisory_unlock(hashtextextended(%s, 0))", [lock_key(environment_id)]
            )
            with pytest.raises(ProfileFenceLostError):
                vault.seal(environment, b"unlocked", fence=lease)
        assert vault.open(environment) == b"held"
    # Another organization's scope: RLS hides the row, so even a held lease writes nothing.
    with tenant_scope(other.organization_id):
        async with locks.hold(environment_id, until=soon(2)) as lease:
            with pytest.raises(ProfileRecordError):
                vault.seal(environment, b"other", fence=lease)
    with tenant_scope(tenant.organization_id):
        assert vault.open(environment) == b"held"
