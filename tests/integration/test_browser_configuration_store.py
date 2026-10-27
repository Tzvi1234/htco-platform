"""Browser configuration profiles against real Postgres (slice 8A.2b, AC-8A.2.10): the save use
case through the real store, the version and its audit in one transaction, the next-version rule,
and the Owner's read through RLS."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.browser_configuration import (
    ConfigurationServices,
    StaleConfigurationError,
    browser_configuration,
    save_browser_configuration,
)
from htco_domain.browser_configuration import ConfigurationRefusedError
from htco_domain.identity import AssuranceLevel, Role
from htco_persistence.browser_configuration import PostgresBrowserConfigurations
from htco_persistence.database import Database
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


class Languages:
    async def recorded(self, auth: AuthContext, shop_id: uuid.UUID) -> tuple[str, ...]:  # noqa: ARG002
        return ("he-IL", "en-US")


def an_environment(admin: Database, tenant: Tenant) -> tuple[uuid.UUID, uuid.UUID]:
    shop, environment = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Configuration test shop', 'etsy', 'active')",
            [shop, tenant.organization_id],
        )
        connection.execute(
            "insert into app.shop_browser_environments (id, organization_id, shop_id, status) "
            "values (%s, %s, %s, 'provisioning')",
            [environment, tenant.organization_id, shop],
        )
    return shop, environment


def owner(tenant: Tenant) -> AuthContext:
    now = datetime.now(UTC)
    return AuthContext(
        user_id=tenant.owner, assurance=AssuranceLevel.AAL2, session_id="s-db",
        memberships=(Membership(tenant.organization_id, Role.OWNER, frozenset()),),
        reauthenticated_at=now - timedelta(minutes=1),
    )  # fmt: skip


@pytest.mark.ac("AC-8A.2.10")
async def test_save__versions_and_their_audit_rows_through_the_real_store(admin: Database) -> None:
    tenant = Tenant(admin)
    shop, environment = an_environment(admin, tenant)
    services = ConfigurationServices(PostgresBrowserConfigurations(admin), Languages())
    now = datetime.now(UTC)

    first = await save_browser_configuration(
        owner(tenant), services, shop, settings={"locale": "he-IL"}, expected_version=0,
        correlation_id="corr-cfg-1", now=now,
    )  # fmt: skip
    second = await save_browser_configuration(
        owner(tenant), services, shop, settings={"locale": "en-us"}, expected_version=1,
        correlation_id="corr-cfg-2", now=now,
    )  # fmt: skip

    assert (first.version, second.version) == (1, 2)
    found = await browser_configuration(owner(tenant), services, shop, now=now)
    assert found == second
    assert found is not None
    assert found.shop_languages == ("he-IL", "en-US")
    assert found.settings.locale == "en-US"
    assert found.settings.egress_id == "shared"
    with admin.connection() as connection:
        audits = connection.execute(
            "select action, entity_id, before, after, correlation_id, actor_kind "
            "from audit.audit_log where entity_id = %s order by correlation_id",
            [str(environment)],
        ).fetchall()
    assert audits == [
        ("browser_configuration.saved", str(environment), None,
         {"version": 1, "locale": "he-IL", "egress_id": "shared"}, "corr-cfg-1", "user"),
        ("browser_configuration.saved", str(environment),
         {"version": 1, "locale": "he-IL", "egress_id": "shared"},
         {"version": 2, "locale": "en-US", "egress_id": "shared"}, "corr-cfg-2", "user"),
    ]  # fmt: skip


@pytest.mark.ac("AC-8A.2.10")
async def test_save__refused__neither_a_version_nor_an_audit_row(admin: Database) -> None:
    tenant = Tenant(admin)
    shop, environment = an_environment(admin, tenant)
    services = ConfigurationServices(PostgresBrowserConfigurations(admin), Languages())

    with pytest.raises(ConfigurationRefusedError):
        await save_browser_configuration(
            owner(tenant), services, shop, settings={"locale": "fr-FR", "proxy": "x"},
            expected_version=0, correlation_id="corr-cfg-refused", now=datetime.now(UTC),
        )  # fmt: skip

    assert (
        scalar(
            admin,
            "select count(*) from app.browser_configuration_profiles where environment_id = %s",
            [environment],
        )
        == 0
    )
    assert (
        scalar(
            admin, "select count(*) from audit.audit_log where entity_id = %s", [str(environment)]
        )
        == 0
    )


async def test_save__two_concurrent_writers_of_one_version__only_one_gets_in(
    admin: Database,
) -> None:
    tenant = Tenant(admin)
    shop, environment = an_environment(admin, tenant)
    services = ConfigurationServices(PostgresBrowserConfigurations(admin), Languages())
    now = datetime.now(UTC)

    async def one(locale: str) -> object:
        return await save_browser_configuration(
            owner(tenant), services, shop, settings={"locale": locale}, expected_version=0,
            correlation_id=f"corr-race-{locale}", now=now,
        )  # fmt: skip

    results = await asyncio.gather(one("he-IL"), one("en-US"), return_exceptions=True)

    assert sorted(type(r).__name__ for r in results) == [
        "ConfigurationVersion", "StaleConfigurationError",
    ]  # fmt: skip
    stale = next(r for r in results if isinstance(r, StaleConfigurationError))
    assert stale.current_version == 1
    assert (
        scalar(
            admin, "select count(*) from audit.audit_log where entity_id = %s", [str(environment)]
        )
        == 1
    )


async def test_store__a_stale_append_writes_nothing(admin: Database) -> None:
    tenant = Tenant(admin)
    shop, _ = an_environment(admin, tenant)
    store = PostgresBrowserConfigurations(admin)
    services = ConfigurationServices(store, Languages())
    saved = await save_browser_configuration(
        owner(tenant), services, shop, settings={"locale": "he-IL"}, expected_version=0,
        correlation_id="corr-stale-1", now=datetime.now(UTC),
    )  # fmt: skip

    from htco_application.audit import AuditRecord  # noqa: PLC0415

    again = AuditRecord.new(
        organization_id=tenant.organization_id, actor=saved.created_by,
        action="browser_configuration.saved", entity_type="browser_environment",
        entity_id=str(saved.environment_id), correlation_id="corr-stale-2",
    )  # fmt: skip
    assert not await store.append(saved, again)  # version 1 again
