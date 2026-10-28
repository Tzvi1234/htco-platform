"""Sealed browser profiles against real Postgres (slice 8A.1): the browser worker's role stores
only ciphertext, an archive copied to another shop's row does not open (AC-8A.1.1), and the role
reaches one organization at a time (AC-8A.1.2)."""

import os
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from htco_application.browser_profiles import (
    NO_ARCHIVE,
    ProfileRecordError,
    ProfileStaleError,
    ProfileVault,
    ProfileWorkspaces,
    SealedArchive,
    SealedProfileProvisioner,
    profile_storage_key,
)
from htco_domain.browser import EnvironmentStatus
from htco_persistence.browser_profiles import PostgresBrowserProfiles
from htco_persistence.database import Database, tenant_scope
from htco_platform.crypto import EnvelopeSealer, KeyRing, SealingError
from tests.integration.test_browser_dispatch_store import lease_for
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

COOKIE = b"integration-cookie-value-never-plain"


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


def an_environment(admin: Database, tenant: Tenant) -> tuple[uuid.UUID, uuid.UUID]:
    shop, environment = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Profile test shop', 'etsy', 'active')",
            [shop, tenant.organization_id],
        )
        connection.execute(
            "insert into app.shop_browser_environments (id, organization_id, shop_id, status) "
            "values (%s, %s, %s, 'provisioning')",
            [environment, tenant.organization_id, shop],
        )
    return shop, environment


@pytest.mark.ac("AC-8A.1.1")
@pytest.mark.ac("AC-8A.1.2")
async def test_profiles__sealed_in_postgres_bound_to_their_shop_and_organization(
    admin: Database, browser: Database, tmp_path: Path
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    shop, environment_id = an_environment(admin, tenant)
    second_shop, second_id = an_environment(admin, tenant)
    foreign_shop, foreign_id = an_environment(admin, other)
    profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, shop)
    )
    sealer = EnvelopeSealer(KeyRing(("browser-1", os.urandom(32))))
    vault = ProfileVault(sealer, profiles, lambda: datetime.now(UTC))
    workspaces = ProfileWorkspaces(vault, tmp_path)

    with tenant_scope(tenant.organization_id):
        result = await SealedProfileProvisioner(profiles, workspaces).provision(environment_id)
        environment = profiles.by_id(environment_id)
        assert environment is not None
        with workspaces.checkout(environment) as directory:
            (directory / "Cookies").write_bytes(COOKIE)
        assert profiles.by_id(foreign_id) is None  # another organization reads as missing

    assert result.status is EnvironmentStatus.LOGIN_REQUIRED
    assert not any(tmp_path.iterdir())  # noqa: ASYNC240 - a test's own temporary directory
    raw = bytes(
        scalar(
            admin,
            "select archive from app.browser_profile_archives where environment_id = %s",
            [environment_id],
        )
    )
    assert COOKIE not in raw
    assert scalar(
        admin,
        "select profile_storage_key from app.shop_browser_environments where id = %s",
        [environment_id],
    ) == profile_storage_key(environment)

    # The same bytes moved to another environment's row: they do not open there.
    with admin.connection() as connection:
        connection.execute(
            """
            insert into app.browser_profile_archives
              (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
            select %s, organization_id, (select shop_id from app.shop_browser_environments
                                         where id = %s), key_id, archive, sealed_at
            from app.browser_profile_archives where environment_id = %s
            """,
            [second_id, second_id, environment_id],
        )
    # Read through a job leased for the second shop, as a session of it would.
    second_profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, second_shop)
    )
    second_vault = ProfileVault(sealer, second_profiles, lambda: datetime.now(UTC))
    with tenant_scope(tenant.organization_id):
        second = second_profiles.by_id(second_id)
        assert second is not None
        with pytest.raises(SealingError):
            second_vault.open(second)

    # A job leased for another organization's shop never reaches this archive (AC-8A.2.23),
    # and the browser role cannot read the shop integrations.
    foreign = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, other.organization_id, foreign_shop)
    )
    assert foreign.get(environment_id) is None
    with tenant_scope(tenant.organization_id), browser.connection() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("select 1 from app.shop_integrations")
        connection.rollback()


async def test_profiles__the_recorded_key_id_follows_every_re_seal(
    admin: Database, browser: Database, tmp_path: Path
) -> None:
    """CODEX (local) on #116: after a key rotation the next session re-seals under the new key,
    and the environment's `profile_key_id` says so, in the archive's own transaction."""
    tenant = Tenant(admin)
    shop, environment_id = an_environment(admin, tenant)
    old_key, new_key = os.urandom(32), os.urandom(32)
    profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, shop)
    )

    def workspaces(ring: KeyRing) -> ProfileWorkspaces:
        vault = ProfileVault(EnvelopeSealer(ring), profiles, lambda: datetime.now(UTC))
        return ProfileWorkspaces(vault, tmp_path)

    with tenant_scope(tenant.organization_id):
        await SealedProfileProvisioner(
            profiles, workspaces(KeyRing(("browser-1", old_key)))
        ).provision(environment_id)
        environment = profiles.by_id(environment_id)
        assert environment is not None
        rotated = workspaces(KeyRing(("browser-2", new_key), [("browser-1", old_key)]))
        with rotated.checkout(environment):
            pass

    with admin.connection() as connection:
        row = connection.execute(
            "select e.profile_key_id, a.key_id from app.shop_browser_environments e "
            "join app.browser_profile_archives a on a.environment_id = e.id where e.id = %s",
            [environment_id],
        ).fetchone()
    assert row == ("browser-2", "browser-2")


async def test_profiles__no_archive_without_its_record_and_no_record_without_its_archive(
    admin: Database, browser: Database
) -> None:
    """CODEX (local) on #116, and AC-8A.2.23: a put or delete through a job leased for another
    organization's shop writes nothing at all."""
    tenant, other = Tenant(admin), Tenant(admin)
    shop, environment_id = an_environment(admin, tenant)
    other_shop, _ = an_environment(admin, other)
    profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, shop)
    )
    foreign = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, other.organization_id, other_shop)
    )
    archive = SealedArchive(
        environment_id=environment_id, organization_id=tenant.organization_id, shop_id=shop,
        key_id="browser-1", blob=b"\x01sealed", sealed_at=datetime.now(UTC),
        storage_key="browser-profiles/x",
    )  # fmt: skip
    with tenant_scope(tenant.organization_id):  # the environment is visible, the lease is not its
        with pytest.raises(ProfileRecordError):
            foreign.put(archive)
        with pytest.raises(ProfileRecordError):
            foreign.delete(environment_id)
    assert scalar(
        admin, "select count(*) from app.browser_profile_archives where environment_id = %s",
        [environment_id],
    ) == 0  # fmt: skip

    with tenant_scope(tenant.organization_id):
        profiles.put(archive)
        profiles.delete(environment_id)
    with admin.connection() as connection:
        row = connection.execute(
            "select profile_storage_key, profile_key_id, (select count(*) from "
            "app.browser_profile_archives where environment_id = e.id) "
            "from app.shop_browser_environments e where id = %s",
            [environment_id],
        ).fetchone()
    assert row == (None, None, 0)


@pytest.mark.ac("AC-8A.2.8")
async def test_profiles__the_generation_compare_and_set_refuses_a_stale_write(
    admin: Database, browser: Database, tmp_path: Path
) -> None:
    """ADR-0025 §1: a session's re-seal replaces only the generation it unpacked. A newer
    archive written meanwhile stays, the stale write raises, and nothing of it is committed:
    neither the archive nor the environment's record."""
    tenant = Tenant(admin)
    shop, environment_id = an_environment(admin, tenant)
    profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, shop)
    )
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), profiles, lambda: datetime.now(UTC)
    )
    workspaces = ProfileWorkspaces(vault, tmp_path)

    with tenant_scope(tenant.organization_id):
        environment = profiles.by_id(environment_id)
        assert environment is not None
        with pytest.raises(ProfileStaleError):
            vault.seal(environment, b"none expected", expected=1)  # nothing stored yet
        workspaces.create(environment)
        assert vault.read(environment)[1] == 1
        checkout = workspaces.checkout(environment)
        directory = checkout.__enter__()
        (directory / "Cookies").write_bytes(b"stale")
        vault.seal(environment, b"newer", expected=1)  # another session's write
        with pytest.raises(ProfileStaleError):
            checkout.__exit__(None, None, None)
        assert vault.read(environment) == (b"newer", 2)
        with pytest.raises(ProfileStaleError):
            vault.seal(environment, b"stale again", expected=NO_ARCHIVE)

    assert (
        scalar(
            admin,
            "select generation from app.browser_profile_archives where environment_id = %s",
            [environment_id],
        )
        == 2
    )
    with admin.connection() as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update app.browser_profile_archives set archive = '\\x01'::bytea "
            "where environment_id = %s",
            [environment_id],
        )


@pytest.mark.ac("AC-8A.2.8")
async def test_profiles__two_writers_at_one_generation_one_wins(
    admin: Database, browser: Database
) -> None:
    """sage-arch on #186: two sessions that both read generation 1 write at the same moment on
    two connections. Exactly one lands (the generation ends at 2); the other raises
    `ProfileStaleError` and commits nothing."""
    tenant = Tenant(admin)
    shop, environment_id = an_environment(admin, tenant)
    profiles = PostgresBrowserProfiles(
        browser, await lease_for(admin, browser, tenant.organization_id, shop)
    )
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), profiles, lambda: datetime.now(UTC)
    )
    with tenant_scope(tenant.organization_id):
        environment = profiles.by_id(environment_id)
        assert environment is not None
        vault.seal(environment, b"first")  # provisioning: generation 1
        assert vault.read(environment)[1] == 1

    start = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def write(blob: bytes) -> None:
        with tenant_scope(tenant.organization_id):
            start.wait(timeout=10)
            try:
                vault.seal(environment, blob, expected=1)
                outcome = "written"
            except ProfileStaleError:
                outcome = "stale"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=write, args=(blob,)) for blob in (b"one", b"two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["stale", "written"]
    with tenant_scope(tenant.organization_id):
        blob, generation = vault.read(environment)
    assert generation == 2
    assert blob in (b"one", b"two")
