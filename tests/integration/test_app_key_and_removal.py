"""Replacing the Etsy app key, and removing a shop, against real Postgres.

The vault records which app key a connection's tokens were issued for. After the key changes, a
connection of the old key reads as `needs_reauth` (the vault, the shop card) and the startup check
stores it with an audit record; no Etsy call is needed for that. Removing a shop deletes its
tokens, probe results, stored orders (items, refunds, buyers), sync state, grants and the shop in
one transaction, with an audit record that names no buyer.
"""

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from htco_application.audit import AuditRecord
from htco_application.shop_integrations import ShopIntegration
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import IntegrationStatus
from htco_integrations.etsy import app_key_fingerprint
from htco_persistence.database import Database
from htco_persistence.etsy_connect import PostgresShopConnector
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.shop_integrations import PostgresIntegrationReader, PostgresTokenVault
from htco_persistence.shops import PostgresShopRemover
from htco_platform.crypto import EnvelopeSealer, KeyRing
from tests.integration.test_etsy_connection import (
    NOW,
    Tenant,
    database,
    etsy_ids,
    request_for,
    scalar,
)
from tests.integration.test_order_history import FULL_NAME, TABLES, count, order

pytestmark = [pytest.mark.db, pytest.mark.anyio]

__all__ = ["database"]  # the fixture, shared with the connection tests

OLD_KEY = app_key_fingerprint(f"old-keystring-{uuid.uuid4().hex}")
NEW_KEY = app_key_fingerprint(f"new-keystring-{uuid.uuid4().hex}")
_RING = KeyRing((f"k-{uuid.uuid4().hex[:8]}", os.urandom(32)), [])


def keyed_vault(database: Database, fingerprint: str | None) -> PostgresTokenVault:
    return PostgresTokenVault(database, EnvelopeSealer(_RING), app_key_fingerprint=fingerprint)


async def connect(database: Database, fingerprint: str | None) -> tuple[Tenant, ShopIntegration]:
    tenant = Tenant(database)
    the_vault = keyed_vault(database, fingerprint)
    shop = await PostgresShopConnector(database, the_vault).connect(request_for(tenant, etsy_ids()))
    integration = await the_vault.describe(shop.shop.id)
    assert integration is not None
    return tenant, integration


def stored_status(database: Database, shop_id: uuid.UUID) -> str:
    return str(
        scalar(database, "select status from app.shop_integrations where shop_id = %s", [shop_id])
    )


def stored_fingerprint(database: Database, shop_id: uuid.UUID) -> str | None:
    value = scalar(
        database,
        "select app_key_fingerprint from app.shop_integrations where shop_id = %s",
        [shop_id],
    )
    return None if value is None else str(value)


# --- The app key ---------------------------------------------------------------------------------


async def test_connect__records_the_fingerprint_of_the_configured_key(database: Database) -> None:
    _, integration = await connect(database, OLD_KEY)

    assert integration.app_key_fingerprint == OLD_KEY
    assert stored_fingerprint(database, integration.shop_id) == OLD_KEY
    assert integration.status is IntegrationStatus.CONNECTED


async def test_key_changed__old_connection_reads_needs_reauth_without_any_call(
    database: Database,
) -> None:
    tenant, integration = await connect(database, OLD_KEY)
    after_change = keyed_vault(database, NEW_KEY)

    loaded = await after_change.load(integration.shop_id)
    described = await after_change.describe(integration.shop_id)
    found = await after_change.find_by_marketplace_shop(integration.etsy_shop_id)

    assert loaded is not None
    assert described is not None
    assert found is not None
    assert {loaded.status, described.status, found.status} == {IntegrationStatus.NEEDS_REAUTH}
    assert not loaded.is_usable
    card = await PostgresIntegrationReader(database, app_key_fingerprint=NEW_KEY).list_integrations(
        tenant.claims(tenant.owner), tenant.organization_id
    )
    assert [c.status for c in card] == [IntegrationStatus.NEEDS_REAUTH]
    # The old key's configuration still sees it connected: only a change of key retires it.
    unchanged = await keyed_vault(database, OLD_KEY).load(integration.shop_id)
    assert unchanged is not None
    assert unchanged.status is IntegrationStatus.CONNECTED


async def test_retire_other_app_keys__stores_needs_reauth_with_one_audit_each(
    database: Database,
) -> None:
    _, old = await connect(database, OLD_KEY)
    _, current = await connect(database, NEW_KEY)
    _, legacy = await connect(database, None)

    retired = await keyed_vault(database, NEW_KEY).retire_other_app_keys()

    assert retired >= 1
    assert stored_status(database, old.shop_id) == "needs_reauth"
    assert stored_status(database, current.shop_id) == "connected"
    assert stored_status(database, legacy.shop_id) == "connected"  # unknown key: left alone
    audits = scalar(
        database,
        """
        select count(*) from audit.audit_log
        where entity_id = %s and action = 'shop.integration_needs_reauth'
          and after ->> 'reason' = 'app_key_changed'
        """,
        [str(old.shop_id)],
    )
    assert audits == 1
    assert await keyed_vault(database, NEW_KEY).retire_other_app_keys() >= 0
    assert (
        scalar(
            database,
            "select count(*) from audit.audit_log where entity_id = %s and action = %s",
            [str(old.shop_id), "shop.integration_needs_reauth"],
        )
        == 1
    )


async def test_retire_other_app_keys__no_configured_key__changes_nothing(
    database: Database,
) -> None:
    _, old = await connect(database, OLD_KEY)

    assert await keyed_vault(database, None).retire_other_app_keys() == 0
    assert stored_status(database, old.shop_id) == "connected"


async def test_legacy_connection__takes_the_current_key_at_its_first_successful_call(
    database: Database,
) -> None:
    _, legacy = await connect(database, None)
    assert stored_fingerprint(database, legacy.shop_id) is None

    await keyed_vault(database, NEW_KEY).record_successful_call(
        legacy.shop_id, at=NOW, generation=legacy.generation
    )

    assert stored_fingerprint(database, legacy.shop_id) == NEW_KEY


@pytest.mark.parametrize("value", ["not-a-fingerprint", "ABCDEF0123456789", "abc"])
async def test_fingerprint__the_column_refuses_anything_but_16_lower_hex(
    database: Database, value: str
) -> None:
    _, integration = await connect(database, NEW_KEY)

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="app_key_fingerprint"),
        database.connection() as connection,
    ):
        connection.execute(
            "update app.shop_integrations set app_key_fingerprint = %s where shop_id = %s",
            [value, integration.shop_id],
        )


# --- Removing a shop -------------------------------------------------------------------------


def removal_audit(integration: ShopIntegration) -> AuditRecord:
    return AuditRecord.new(
        organization_id=integration.organization_id,
        actor=ActorRef(ActorKind.USER, str(uuid.uuid4())),
        action="shop.removed",
        entity_type="shop",
        entity_id=str(integration.shop_id),
        after={"removed": True},
    )


async def test_remove__deletes_tokens_orders_buyers_sync_state_grants_and_the_shop(
    database: Database,
) -> None:
    tenant, integration = await connect(database, NEW_KEY)
    _, neighbour = await connect(database, NEW_KEY)
    store = PostgresOrderStore(database)
    await store.save_backfill_page(integration, [order(71), order(72)], next_offset=None, now=NOW)
    await store.save_backfill_page(neighbour, [order(73)], next_offset=None, now=NOW)
    shop = integration.shop_id
    with database.connection() as connection:
        connection.execute(
            """
            insert into app.shop_access (id, organization_id, membership_id, shop_id)
            values (%s, %s, %s, %s)
            """,
            [uuid.uuid4(), tenant.organization_id, tenant.member_membership, shop],
        )
        connection.execute(
            """
            insert into app.shop_capabilities
              (id, organization_id, shop_id, capability, state, detail, checked_at)
            values (%s, %s, %s, 'shop_details', 'available', 'read_ok', %s)
            """,
            [uuid.uuid4(), tenant.organization_id, shop, datetime.now(UTC)],
        )
    remover = PostgresShopRemover(database)
    found = await remover.find(shop)
    assert found is not None
    assert found.integration_status is IntegrationStatus.CONNECTED

    removed = await remover.remove(found, audit=removal_audit(integration))

    assert removed is not None
    assert removed.orders_deleted == 2
    assert [count(database, t, shop) for t in TABLES] == [0, 0, 0, 0, 0]
    for table in ("shop_integrations", "shop_capabilities", "shop_access"):
        assert count(database, table, shop) == 0
    assert scalar(database, "select count(*) from app.shops where id = %s", [shop]) == 0
    assert count(database, "etsy_orders", neighbour.shop_id) == 1  # other shops keep theirs
    record = scalar(
        database,
        "select after::text from audit.audit_log where entity_id = %s and action = 'shop.removed'",
        [str(shop)],
    )
    assert '"orders_deleted": 2' in record
    assert FULL_NAME not in record
    assert await remover.find(shop) is None
    assert await remover.remove(found, audit=removal_audit(integration)) is None  # idempotent


async def test_remove__a_disconnected_shop_without_orders(database: Database) -> None:
    _, integration = await connect(database, NEW_KEY)
    await keyed_vault(database, NEW_KEY).disconnect(integration.shop_id)
    remover = PostgresShopRemover(database)
    found = await remover.find(integration.shop_id)
    assert found is not None
    assert found.integration_status is IntegrationStatus.DISCONNECTED

    removed = await remover.remove(found, audit=removal_audit(integration))

    assert removed is not None
    assert removed.orders_deleted == 0
    assert (
        scalar(database, "select count(*) from app.shops where id = %s", [integration.shop_id]) == 0
    )
