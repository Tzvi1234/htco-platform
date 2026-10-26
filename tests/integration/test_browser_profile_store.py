"""Sealed browser profiles against real Postgres (slice 8A.1): the browser worker's role stores
only ciphertext, an archive copied to another shop's row does not open (AC-8A.1.1), and the role
reaches one organization at a time (AC-8A.1.2)."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from htco_application.browser_profiles import (
    ProfileVault,
    ProfileWorkspaces,
    SealedProfileProvisioner,
    profile_storage_key,
)
from htco_domain.browser import EnvironmentStatus
from htco_persistence.browser_profiles import PostgresBrowserProfiles
from htco_persistence.database import Database, tenant_scope
from htco_platform.crypto import EnvelopeSealer, KeyRing, SealingError
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
    _, environment_id = an_environment(admin, tenant)
    _, second_id = an_environment(admin, tenant)
    _, foreign_id = an_environment(admin, other)
    profiles = PostgresBrowserProfiles(browser)
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), profiles, lambda: datetime.now(UTC)
    )
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
    with tenant_scope(tenant.organization_id):
        second = profiles.by_id(second_id)
        assert second is not None
        with pytest.raises(SealingError):
            vault.open(second)

    # The browser role cannot read another organization's archive or the shop integrations.
    with tenant_scope(other.organization_id):
        assert profiles.get(environment_id) is None
    with tenant_scope(tenant.organization_id), browser.connection() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("select 1 from app.shop_integrations")
        connection.rollback()


async def test_profiles__the_recorded_key_id_follows_every_re_seal(
    admin: Database, browser: Database, tmp_path: Path
) -> None:
    """quill on #116: after a key rotation the next session re-seals under the new key, and the
    environment's `profile_key_id` says so, in the same transaction as the archive."""
    tenant = Tenant(admin)
    _, environment_id = an_environment(admin, tenant)
    old_key, new_key = os.urandom(32), os.urandom(32)
    profiles = PostgresBrowserProfiles(browser)

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

    def recorded() -> tuple[str, str]:
        with admin.connection() as connection:
            row = connection.execute(
                "select e.profile_key_id, a.key_id from app.shop_browser_environments e "
                "join app.browser_profile_archives a on a.environment_id = e.id where e.id = %s",
                [environment_id],
            ).fetchone()
        assert row is not None
        return row[0], row[1]

    assert recorded() == ("browser-2", "browser-2")
