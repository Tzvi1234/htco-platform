"""Capability results and disconnect against real Postgres (slices 4.5 and 4.7).

Every test creates its own users, organization and Etsy IDs, so tests never share rows.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import anyio
import pytest

from htco_application.audit import AuditRecord
from htco_application.shop_capabilities import ProbeSupersededError
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


async def the_generation(database: Database, shop_id: uuid.UUID) -> int:
    """The shop's current connection generation, as a probe would have read it."""
    return int(
        scalar(
            database,
            "select connection_generation from app.shop_integrations where shop_id = %s",
            [shop_id],
        )
    )


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

    generation = connected.integration.generation
    await store.save(
        tenant.organization_id, shop, results(NOW - timedelta(hours=1)), generation=generation
    )
    await store.save(tenant.organization_id, shop, results(NOW), generation=generation)

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
    await store.save(
        tenant.organization_id,
        connected.shop.id,
        results(),
        generation=(await the_generation(database, connected.shop.id)),
    )

    with pytest.raises(ValueError, match="another organization"):
        await store.save(
            other.organization_id,
            connected.shop.id,
            results(NOW + timedelta(1)),
            generation=connected.integration.generation,
        )

    assert (
        scalar(
            database,
            "select max(checked_at) from app.shop_capabilities where shop_id = %s",
            [connected.shop.id],
        )
        == NOW
    )


async def test_capability_store__an_older_probe_never_overwrites_a_newer_one(
    database: Database,
) -> None:
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    await store.save(
        tenant.organization_id,
        connected.shop.id,
        results(NOW),
        generation=(await the_generation(database, connected.shop.id)),
    )

    await store.save(
        tenant.organization_id,
        connected.shop.id,
        results(NOW - timedelta(hours=1)),
        generation=(await the_generation(database, connected.shop.id)),
    )

    assert (
        scalar(
            database,
            "select min(checked_at) from app.shop_capabilities where shop_id = %s",
            [connected.shop.id],
        )
        == NOW
    )


async def test_capability_store__reserve_check__one_winner_then_the_wait(
    database: Database,
) -> None:
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    cooldown = timedelta(seconds=30)
    generation = connected.integration.generation
    answers: list[timedelta | None] = []

    async def reserve() -> None:
        answers.append(
            await store.reserve_check(
                connected.shop.id, now=NOW, cooldown=cooldown, generation=generation
            )
        )

    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(reserve)

    assert answers.count(None) == 1
    assert sorted(a for a in answers if a is not None) == [cooldown] * 3
    later = await store.reserve_check(
        connected.shop.id, now=NOW + cooldown, cooldown=cooldown, generation=generation
    )
    assert later is None


async def test_reconnect_and_disconnect__drop_the_old_probe_results(database: Database) -> None:
    """Results of an earlier connection never show for a new one (Codex review of #49)."""
    tenant = Tenant(database)
    the_vault = vault(database)
    etsy = etsy_ids()
    connector = PostgresShopConnector(database, the_vault)
    connected = await connector.connect(request_for(tenant, etsy))
    store = PostgresCapabilityStore(database)
    count = "select count(*) from app.shop_capabilities where shop_id = %s"

    await store.save(
        tenant.organization_id,
        connected.shop.id,
        results(),
        generation=(await the_generation(database, connected.shop.id)),
    )
    await connector.connect(request_for(tenant, etsy))
    assert scalar(database, count, [connected.shop.id]) == 0

    await store.save(
        tenant.organization_id,
        connected.shop.id,
        results(),
        generation=(await the_generation(database, connected.shop.id)),
    )
    await the_vault.disconnect(connected.shop.id, purge_after=NOW + timedelta(days=30))
    assert scalar(database, count, [connected.shop.id]) == 0


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


async def test_capability_store__a_probe_of_an_earlier_connection_is_dropped(
    database: Database,
) -> None:
    """A probe still running when the shop is disconnected or reconnected never stores its
    results for the new state (Codex review of #49)."""
    tenant = Tenant(database)
    the_vault = vault(database)
    etsy = etsy_ids()
    connector = PostgresShopConnector(database, the_vault)
    first = await connector.connect(request_for(tenant, etsy))
    store = PostgresCapabilityStore(database)
    shop = first.shop.id
    count = "select count(*) from app.shop_capabilities where shop_id = %s"

    await the_vault.disconnect(shop, purge_after=NOW + timedelta(days=30))
    late = await store.save(
        tenant.organization_id, shop, results(), generation=first.integration.generation
    )
    assert late is False
    assert scalar(database, count, [shop]) == 0

    again = await connector.connect(request_for(tenant, etsy))
    assert again.integration.generation > first.integration.generation
    assert not await store.save(
        tenant.organization_id, shop, results(), generation=first.integration.generation
    )
    assert scalar(database, count, [shop]) == 0
    assert await store.save(
        tenant.organization_id, shop, results(), generation=again.integration.generation
    )
    assert scalar(database, count, [shop]) == len(results())


async def test_capability_store__success_time_only_with_the_current_generation(
    database: Database,
) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    connected = await PostgresShopConnector(database, the_vault).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    shop = connected.shop.id
    stale = connected.integration.generation - 1

    assert not await store.save(
        tenant.organization_id, shop, results(), generation=stale, successful_call_at=NOW
    )
    loaded = await the_vault.load(shop)
    assert loaded is not None
    assert loaded.last_successful_call_at is None

    assert await store.save(
        tenant.organization_id,
        shop,
        results(),
        generation=connected.integration.generation,
        successful_call_at=NOW,
    )
    loaded = await the_vault.load(shop)
    assert loaded is not None
    assert loaded.last_successful_call_at == NOW


async def test_describe__never_opens_the_tokens_so_a_lost_key_can_still_disconnect(
    database: Database,
) -> None:
    """Codex review of #49."""
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    shop = connected.shop.id
    stranger = vault(database)  # a vault whose key ring does not hold this row's key

    described = await stranger.describe(shop)

    assert described is not None
    assert (described.status, described.tokens) == (IntegrationStatus.CONNECTED, None)
    assert described.organization_id == tenant.organization_id
    assert await stranger.disconnect(shop, purge_after=NOW + timedelta(days=30))


async def test_capability_store__results_and_their_audit_commit_together(
    database: Database,
) -> None:
    """Codex review of #49: if the audit record cannot be written, nothing of the save stays."""
    tenant = Tenant(database)
    the_vault = vault(database)
    connected = await PostgresShopConnector(database, the_vault).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    shop = connected.shop.id
    record = AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=ActorRef(ActorKind.USER, str(tenant.owner)),
        action="shop.capabilities_checked",
        entity_type="shop",
        entity_id=str(shop),
        after={"trigger": "recheck"},
    )
    generation = connected.integration.generation
    assert await store.save(
        tenant.organization_id, shop, results(NOW), generation=generation, audit=record
    )

    with pytest.raises(Exception, match="audit_log_pkey"):  # the same record twice
        await store.save(
            tenant.organization_id,
            shop,
            results(NOW + timedelta(hours=1)),
            generation=generation,
            successful_call_at=NOW + timedelta(hours=1),
            audit=record,
        )

    assert (
        scalar(
            database, "select max(checked_at) from app.shop_capabilities where shop_id = %s", [shop]
        )
        == NOW
    )
    loaded = await the_vault.load(shop)
    assert loaded is not None
    assert loaded.last_successful_call_at is None
    assert scalar(database, "select count(*) from audit.audit_log where id = %s", [record.id]) == 1


async def test_capability_store__an_older_probe_of_the_same_connection_leaves_no_trace(
    database: Database,
) -> None:
    """Codex review of #49: a probe that a newer one overtook (same connection) stores nothing,
    sets no success time and writes no audit; its caller is told it was superseded."""
    tenant = Tenant(database)
    the_vault = vault(database)
    connected = await PostgresShopConnector(database, the_vault).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    shop = connected.shop.id
    generation = connected.integration.generation
    newer = NOW + timedelta(minutes=1)
    assert await store.save(tenant.organization_id, shop, results(newer), generation=generation)
    late = AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=ActorRef(ActorKind.USER, str(tenant.owner)),
        action="shop.capabilities_checked",
        entity_type="shop",
        entity_id=str(shop),
        after={"trigger": "recheck"},
    )

    saved = await store.save(
        tenant.organization_id,
        shop,
        results(NOW),
        generation=generation,
        successful_call_at=NOW,
        audit=late,
    )

    assert saved is False
    assert (
        scalar(
            database, "select min(checked_at) from app.shop_capabilities where shop_id = %s", [shop]
        )
        == newer
    )
    loaded = await the_vault.load(shop)
    assert loaded is not None
    assert loaded.last_successful_call_at is None
    assert scalar(database, "select count(*) from audit.audit_log where id = %s", [late.id]) == 0


async def test_capability_store__reserve_check__only_for_the_loaded_connection(
    database: Database,
) -> None:
    """Codex review of #49: a re-check whose connection was replaced makes no Etsy call."""
    tenant = Tenant(database)
    the_vault = vault(database)
    connected = await PostgresShopConnector(database, the_vault).connect(
        request_for(tenant, etsy_ids())
    )
    store = PostgresCapabilityStore(database)
    shop = connected.shop.id
    await the_vault.disconnect(shop, purge_after=NOW + timedelta(days=30))

    with pytest.raises(ProbeSupersededError):
        await store.reserve_check(
            shop,
            now=NOW,
            cooldown=timedelta(seconds=30),
            generation=connected.integration.generation,
        )
