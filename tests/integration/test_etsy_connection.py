"""The Etsy connect flow and token refresh against real Postgres (slices 4.3 and 4.4).

Every test creates its own users, organization and Etsy IDs, so tests never share rows.
"""

import base64
import os
import random
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest

from htco_application.audit import AuditRecord
from htco_application.etsy_connect import (
    EtsyShopConnectedElsewhereError,
    PendingAuthorization,
    ShopConnectionRequest,
)
from htco_application.etsy_tokens import VaultAccessTokenProvider
from htco_application.shop_integrations import OAuthTokens
from htco_application.shops import ShopQuery
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import IntegrationStatus
from htco_persistence.audit import PostgresAuditLog
from htco_persistence.database import Database
from htco_persistence.etsy_connect import PostgresPendingAuthorizations, PostgresShopConnector
from htco_persistence.shop_integrations import (
    PostgresIntegrationReader,
    PostgresRefreshLock,
    PostgresTokenVault,
)
from htco_persistence.shops import PostgresShopReader
from htco_platform.crypto import EnvelopeSealer, KeyRing, Sealer
from tests.etsy_fakes import AuthorizationServer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})


def _url() -> str:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set; start the database with `just db-start`")
    return url


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def vault(database: Database) -> PostgresTokenVault:
    ring = KeyRing((f"k-{uuid.uuid4().hex[:8]}", os.urandom(32)), [])
    return PostgresTokenVault(database, EnvelopeSealer(ring))


class Tenant:
    """An organization with its owner, an operations member and the owner's web session."""

    def __init__(self, database: Database) -> None:
        self.organization_id = uuid.uuid4()
        self.owner = uuid.uuid4()
        self.member = uuid.uuid4()
        self.owner_membership = uuid.uuid4()
        self.member_membership = uuid.uuid4()
        self.session_id = uuid.uuid4()
        with database.connection() as connection:
            for user in (self.owner, self.member):
                connection.execute(
                    "insert into auth.users (id, email) values (%s, %s)",
                    [user, f"etsy-{user.hex[:12]}@example.test"],
                )
            connection.execute(
                "insert into app.organizations (id, name) values (%s, 'Etsy connect test')",
                [self.organization_id],
            )
            for membership, user, role in (
                (self.owner_membership, self.owner, "owner"),
                (self.member_membership, self.member, "operations"),
            ):
                connection.execute(
                    """
                    insert into app.memberships (id, organization_id, user_id, role)
                    values (%s, %s, %s, %s)
                    """,
                    [membership, self.organization_id, user, role],
                )
            connection.execute(
                """
                insert into private.web_sessions
                  (id, token_hash, user_id, access_token, refresh_token, access_expires_at,
                   idle_expires_at, absolute_expires_at)
                values (%s, %s, %s, '\\x01', '\\x01', now(), now() + interval '1 hour',
                        now() + interval '1 day')
                """,
                [self.session_id, os.urandom(32), self.owner],
            )

    def claims(self, user: uuid.UUID, aal: str = "aal2") -> dict[str, Any]:
        return {"sub": str(user), "role": "authenticated", "aal": aal}


def etsy_ids() -> tuple[int, int]:
    return random.randint(10**8, 10**9), random.randint(10**8, 10**9)  # noqa: S311 - test IDs


def tokens(label: str = "", *, access_left: timedelta = timedelta(hours=1)) -> OAuthTokens:
    marker = uuid.uuid4().hex
    return OAuthTokens(
        access_token=f"98765432.access{label}-{marker}",
        refresh_token=f"98765432.refresh{label}-{marker}",
        access_expires_at=NOW + access_left,
        refresh_expires_at=NOW + timedelta(days=90),
    )


def request_for(
    tenant: Tenant,
    etsy: tuple[int, int],
    *,
    scopes: frozenset[str] = SCOPES,
    name: str = "Connect Test Shop",
    access_left: timedelta = timedelta(hours=1),
) -> ShopConnectionRequest:
    return ShopConnectionRequest(
        organization_id=tenant.organization_id,
        owner_user_id=tenant.owner,
        etsy_shop_id=etsy[0],
        etsy_user_id=etsy[1],
        shop_name=name,
        scopes=scopes,
        tokens=tokens(access_left=access_left),
        correlation_id="req-db",
    )


def scalar(database: Database, sql: str, params: list[object]) -> Any:
    with database.connection() as connection:
        row = connection.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


# --- Pending authorizations --------------------------------------------------------------------


def pending_for(tenant: Tenant, *, state_hash: bytes, verifier: str) -> PendingAuthorization:
    return PendingAuthorization(
        state_hash=state_hash,
        organization_id=tenant.organization_id,
        user_id=tenant.owner,
        session_id=tenant.session_id,
        code_verifier=verifier,
        scopes=SCOPES,
        redirect_uri="https://dash.ht-co.app/integrations/etsy/callback",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )


@pytest.mark.ac("AC-4.3.1")
async def test_pending__taken_once_and_the_verifier_is_sealed_at_rest(database: Database) -> None:
    tenant = Tenant(database)
    store = PostgresPendingAuthorizations(database, Sealer(os.urandom(32)))
    state_hash, verifier = os.urandom(32), f"verifier-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    await store.save(pending_for(tenant, state_hash=state_hash, verifier=verifier))

    text = scalar(
        database,
        "select t::text from private.oauth_authorizations t where state_hash = %s",
        [state_hash],
    )
    first = await store.take(state_hash)
    second = await store.take(state_hash)

    for form in (verifier, verifier.encode().hex(), base64.b64encode(verifier.encode()).decode()):
        assert form not in text
    assert first is not None
    assert first.code_verifier == verifier
    assert (first.user_id, first.session_id, first.scopes) == (
        tenant.owner,
        tenant.session_id,
        SCOPES,
    )
    assert first.expires_at == NOW + timedelta(minutes=10)
    assert second is None


@pytest.mark.ac("AC-4.3.1")
async def test_pending__concurrent_takes__exactly_one_wins(database: Database) -> None:
    tenant = Tenant(database)
    store = PostgresPendingAuthorizations(database, Sealer(os.urandom(32)))
    state_hash = os.urandom(32)
    await store.save(pending_for(tenant, state_hash=state_hash, verifier="v" * 64))
    won: list[bool] = []

    async def take() -> None:
        won.append(await store.take(state_hash) is not None)

    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(take)

    assert sorted(won) == [False, False, False, True]


async def test_pending__saving_drops_the_users_expired_authorizations(database: Database) -> None:
    tenant = Tenant(database)
    store = PostgresPendingAuthorizations(database, Sealer(os.urandom(32)))
    old = pending_for(tenant, state_hash=os.urandom(32), verifier="o" * 64)
    old = replace(old, created_at=NOW - timedelta(hours=1), expires_at=NOW - timedelta(minutes=50))
    await store.save(old)

    await store.save(pending_for(tenant, state_hash=os.urandom(32), verifier="n" * 64))

    count = scalar(
        database,
        "select count(*) from private.oauth_authorizations where user_id = %s",
        [tenant.owner],
    )
    assert count == 1


# --- Connecting --------------------------------------------------------------------------------


@pytest.mark.ac("AC-4.3.3")
async def test_connect__creates_the_shop_integration_owner_grant_and_audit_in_one_go(
    database: Database,
) -> None:
    tenant = Tenant(database)
    connector = PostgresShopConnector(database, vault(database))
    etsy = etsy_ids()

    connected = await connector.connect(request_for(tenant, etsy))

    assert connected.reconnected is False
    assert (connected.shop.status, connected.shop.name) == ("active", "Connect Test Shop")
    assert connected.integration.status is IntegrationStatus.CONNECTED
    assert connected.integration.scopes == SCOPES
    grants = scalar(
        database,
        "select array_agg(membership_id) from app.shop_access where shop_id = %s",
        [connected.shop.id],
    )
    assert grants == [tenant.owner_membership]
    audit = scalar(
        database,
        """
        select array_agg(action || ':' || actor_kind || ':' || actor_id || ':' || correlation_id)
        from audit.audit_log where entity_id = %s
        """,
        [str(connected.shop.id)],
    )
    assert audit == [f"shop.connected:user:{tenant.owner}:req-db"]
    member_view = await PostgresShopReader(database).list_shops(
        tenant.claims(tenant.member),
        ShopQuery(tenant.organization_id, frozenset(), None, None, 10),
    )
    assert member_view == []


@pytest.mark.ac("AC-4.3.2")
async def test_connect__same_etsy_shop_twice__updates_one_shop(database: Database) -> None:
    tenant = Tenant(database)
    connector = PostgresShopConnector(database, vault(database))
    etsy = etsy_ids()

    first = await connector.connect(request_for(tenant, etsy, name="Old name"))
    second = await connector.connect(request_for(tenant, etsy, name="New name"))

    assert second.reconnected is True
    assert second.shop.id == first.shop.id
    shops = scalar(
        database,
        "select array_agg(name) from app.shops where organization_id = %s",
        [tenant.organization_id],
    )
    assert shops == ["New name"]
    grants = scalar(
        database, "select count(*) from app.shop_access where shop_id = %s", [first.shop.id]
    )
    assert grants == 1


@pytest.mark.ac("AC-4.3.4")
async def test_connect__reconnect_with_more_scopes__same_record_new_scopes(
    database: Database,
) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    connector = PostgresShopConnector(database, the_vault)
    etsy = etsy_ids()
    first = await connector.connect(request_for(tenant, etsy, scopes=frozenset({"shops_r"})))

    more = SCOPES | {"listings_w"}
    second = await connector.connect(request_for(tenant, etsy, scopes=more))

    loaded = await the_vault.load(first.shop.id)
    assert loaded is not None
    assert loaded.id == first.integration.id
    assert loaded.scopes == more
    assert second.integration.scopes == more
    before = scalar(
        database,
        "select before from audit.audit_log where entity_id = %s and action = 'shop.reconnected'",
        [str(first.shop.id)],
    )
    assert before == {"scopes": ["shops_r"], "status": "connected"}


async def test_connect__etsy_shop_connected_in_another_organization__refused(
    database: Database,
) -> None:
    first, second = Tenant(database), Tenant(database)
    the_vault = vault(database)
    connector = PostgresShopConnector(database, the_vault)
    etsy = etsy_ids()
    connected = await connector.connect(request_for(first, etsy))

    with pytest.raises(EtsyShopConnectedElsewhereError):
        await connector.connect(request_for(second, etsy))

    shops = scalar(
        database,
        "select count(*) from app.shops where organization_id = %s",
        [second.organization_id],
    )
    assert shops == 0
    await the_vault.disconnect(connected.shop.id)
    allowed = await connector.connect(request_for(second, etsy))
    assert allowed.shop.organization_id == second.organization_id


# --- Integrations for the shop card -------------------------------------------------------------


async def test_integration_reader__owner_sees_the_card_fields_member_sees_nothing(
    database: Database,
) -> None:
    tenant = Tenant(database)
    connected = await PostgresShopConnector(database, vault(database)).connect(
        request_for(tenant, etsy_ids())
    )
    reader = PostgresIntegrationReader(database)

    owner_view = await reader.list_integrations(tenant.claims(tenant.owner), tenant.organization_id)
    member_view = await reader.list_integrations(
        tenant.claims(tenant.member), tenant.organization_id
    )

    [summary] = owner_view
    assert summary.shop_id == connected.shop.id
    assert summary.scopes == SCOPES
    assert summary.status is IntegrationStatus.CONNECTED
    assert summary.refresh_expires_at == NOW + timedelta(days=90)
    assert member_view == []


# --- Refresh (slice 4.4) -----------------------------------------------------------------------


@pytest.mark.ac("AC-4.4.1")
async def test_refresh__ten_concurrent_requests_in_two_processes__one_refresh_call() -> None:
    first_db = Database(_url(), min_size=1, max_size=3)
    second_db = Database(_url(), min_size=1, max_size=3)
    await first_db.start()
    await second_db.start()
    try:
        tenant = Tenant(first_db)
        ring = KeyRing((f"k-{uuid.uuid4().hex[:8]}", os.urandom(32)), [])
        vaults = [PostgresTokenVault(db, EnvelopeSealer(ring)) for db in (first_db, second_db)]
        etsy = etsy_ids()
        connected = await PostgresShopConnector(first_db, vaults[0]).connect(
            request_for(tenant, etsy, access_left=-timedelta(minutes=1))
        )
        server = AuthorizationServer(refresh_delay=0.2)
        providers = [
            VaultAccessTokenProvider(v, server, PostgresRefreshLock(db), clock=lambda: NOW)
            for v, db in zip(vaults, (first_db, second_db), strict=True)
        ]
        seen: list[str] = []

        async def call(index: int) -> None:
            seen.append(await providers[index % 2].access_token(etsy[0]))

        async with anyio.create_task_group() as group:
            for index in range(10):
                group.start_soon(call, index)

        assert len(server.refreshed) == 1
        assert len(set(seen)) == 1
        loaded = await vaults[0].load(connected.shop.id)
        assert loaded is not None
        assert loaded.tokens is not None
        assert loaded.tokens.access_token == seen[0]
        assert connected.integration.tokens is not None
        assert loaded.tokens.refresh_token != connected.integration.tokens.refresh_token
    finally:
        await first_db.stop()
        await second_db.stop()


@pytest.mark.ac("AC-4.4.2")
async def test_mark_needs_reauth__with_audit__once_and_atomically(database: Database) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    connected = await PostgresShopConnector(database, the_vault).connect(
        request_for(tenant, etsy_ids())
    )

    def record() -> AuditRecord:
        return AuditRecord.new(
            organization_id=tenant.organization_id,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-token-refresh"),
            action="shop.integration_needs_reauth",
            entity_type="shop",
            entity_id=str(connected.shop.id),
            after={"reason": "invalid_grant", "provider": "etsy"},
        )

    first = await the_vault.mark_needs_reauth(connected.shop.id, audit=record())
    second = await the_vault.mark_needs_reauth(connected.shop.id, audit=record())

    assert (first, second) == (True, False)
    loaded = await the_vault.load(connected.shop.id)
    assert loaded is not None
    assert loaded.status is IntegrationStatus.NEEDS_REAUTH
    count = scalar(
        database,
        """
        select count(*) from audit.audit_log
        where entity_id = %s and action = 'shop.integration_needs_reauth'
        """,
        [str(connected.shop.id)],
    )
    assert count == 1


async def test_find_by_marketplace_shop__the_connected_row_or_none(database: Database) -> None:
    tenant = Tenant(database)
    the_vault = vault(database)
    etsy = etsy_ids()
    connected = await PostgresShopConnector(database, the_vault).connect(request_for(tenant, etsy))

    found = await the_vault.find_by_marketplace_shop(etsy[0])
    await the_vault.disconnect(connected.shop.id)
    gone = await the_vault.find_by_marketplace_shop(etsy[0])

    assert found is not None
    assert found.shop_id == connected.shop.id
    assert gone is None


async def test_audit_log__appends_outside_a_change(database: Database) -> None:
    tenant = Tenant(database)
    entity = str(uuid.uuid4())

    await PostgresAuditLog(database).append(
        AuditRecord.new(
            organization_id=tenant.organization_id,
            actor=ActorRef(ActorKind.USER, str(tenant.owner)),
            action="shop.connect_rejected",
            entity_type="etsy_authorization",
            entity_id=entity,
            after={"reason": "expired"},
        )
    )

    after = scalar(database, "select after from audit.audit_log where entity_id = %s", [entity])
    assert after == {"reason": "expired"}
