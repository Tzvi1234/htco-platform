"""Capability results and disconnect against real Postgres (slices 4.5 and 4.7).

Every test creates its own users, organization and Etsy IDs, so tests never share rows.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import (
    CapabilityResult,
    CapabilityState,
    IntegrationStatus,
    ShopCapability,
)
from htco_persistence.database import Database
from htco_persistence.etsy_connect import PostgresShopConnector
from htco_persistence.shop_integrations import PostgresCapabilityStore, PostgresIntegrationReader
from tests.integration.test_etsy_connection import (
    NOW,
    Tenant,
    _url,
    etsy_ids,
    request_for,
    scalar,
    vault,
)

pytestmark = [pytest.mark.db, pytest.mark.anyio]


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def results(at: datetime = NOW) -> list[CapabilityResult]:
    return [
        CapabilityResult(ShopCapability.SHOP_DETAILS, CapabilityState.AVAILABLE, "read_ok", at),
        CapabilityResult(
            ShopCapability.SHIPMENT_WRITE, CapabilityState.UNKNOWN, "no_safe_probe", at
        ),
    ]


@pytest.mark.ac("AC-4.5.1")
async def test_capability_store__keeps_the_latest_per_capability_and_owner_reads_them(
    database: Database,
) -> None:
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    shop = connected.shop.id

    await store.save(tenant.organization_id, shop, results(NOW - timedelta(hours=1)))
    await store.save(tenant.organization_id, shop, results(NOW))

    assert await store.last_checked_at(shop) == NOW
    assert scalar(
        database, "select count(*) from app.shop_capabilities where shop_id = %s", [shop]
    ) == len(results())
    reader = PostgresIntegrationReader(database)
    [owner_view] = await reader.list_integrations(
        tenant.claims(tenant.owner), tenant.organization_id
    )
    assert [(c.capability, c.state, c.detail, c.checked_at) for c in owner_view.capabilities] == [
        (ShopCapability.SHOP_DETAILS, CapabilityState.AVAILABLE, "read_ok", NOW),
        (ShopCapability.SHIPMENT_WRITE, CapabilityState.UNKNOWN, "no_safe_probe", NOW),
    ]
    member_view = await reader.list_integrations(
        tenant.claims(tenant.member), tenant.organization_id
    )
    assert member_view == []


async def test_capability_store__never_moves_a_row_to_another_organization(
    database: Database,
) -> None:
    tenant, other = Tenant(database), Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    await store.save(tenant.organization_id, connected.shop.id, results())

    with pytest.raises(Exception, match="foreign key"):
        await store.save(other.organization_id, connected.shop.id, results())


async def test_capability_store__never_checked__none(database: Database) -> None:
    assert await PostgresCapabilityStore(database).last_checked_at(uuid.uuid4()) is None


def disconnect_record(tenant: Tenant, shop: uuid.UUID) -> AuditRecord:
    return AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=ActorRef(ActorKind.USER, str(tenant.owner)),
        action="shop.disconnected",
        entity_type="shop",
        entity_id=str(shop),
        after={"status": "disconnected"},
    )


@pytest.mark.ac("AC-4.7.1")
async def test_disconnect__tokens_gone_shop_disconnected_purge_date_and_audit_at_once(
    database: Database,
) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    etsy = etsy_ids()
    connected = await PostgresShopConnector(database, the_vault).connect(request_for(tenant, etsy))
    shop = connected.shop.id
    purge = NOW + timedelta(days=30)

    first = await the_vault.disconnect(
        shop, purge_after=purge, audit=disconnect_record(tenant, shop)
    )
    second = await the_vault.disconnect(
        shop, purge_after=purge, audit=disconnect_record(tenant, shop)
    )

    assert (first, second) == (True, False)
    loaded = await the_vault.load(shop)
    assert loaded is not None
    assert loaded.status is IntegrationStatus.DISCONNECTED
    assert loaded.tokens is None
    assert loaded.purge_after == purge
    assert loaded.disconnected_at is not None
    assert scalar(database, "select status from app.shops where id = %s", [shop]) == (
        "disconnected"
    )
    assert (
        scalar(
            database,
            """
        select count(*) from app.shop_integrations
        where shop_id = %s and access_token_ciphertext is null
          and refresh_token_ciphertext is null and wrapped_data_key is null
        """,
            [shop],
        )
        == 1
    )
    assert (
        scalar(
            database,
            "select count(*) from audit.audit_log where entity_id = %s and action = %s",
            [str(shop), "shop.disconnected"],
        )
        == 1
    )
    # The token provider's lookup no longer finds the shop: no Etsy call can be made for it.
    assert await the_vault.find_by_marketplace_shop(etsy[0]) is None
    [view] = await PostgresIntegrationReader(database).list_integrations(
        tenant.claims(tenant.owner), tenant.organization_id
    )
    assert (view.status, view.purge_after) == (IntegrationStatus.DISCONNECTED, purge)


async def test_reconnect_after_disconnect__clears_the_purge_date(database: Database) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    etsy = etsy_ids()
    connector = PostgresShopConnector(database, the_vault)
    connected = await connector.connect(request_for(tenant, etsy))
    await the_vault.disconnect(connected.shop.id, purge_after=NOW + timedelta(days=30))

    again = await connector.connect(request_for(tenant, etsy))

    assert again.shop.id == connected.shop.id
    assert again.shop.status == "active"
    assert again.integration.status is IntegrationStatus.CONNECTED
    assert (again.integration.disconnected_at, again.integration.purge_after) == (None, None)


async def test_disconnect_dates__only_on_a_disconnected_row(database: Database) -> None:
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )

    with (
        pytest.raises(Exception, match="shop_integrations_disconnect_dates_check"),
        database.connection() as connection,
    ):
        connection.execute(
            "update app.shop_integrations set purge_after = now() where shop_id = %s",
            [connected.shop.id],
        )
