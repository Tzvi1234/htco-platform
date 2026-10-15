"""The Etsy connect flow's storage (slice 4.3): pending authorizations and the connection itself.

Both run with the API's login role. `private.oauth_authorizations` is granted to no client role;
the shop, the owner's grant, the sealed tokens and the audit record are written in one transaction
after the application has checked the caller, because `app.shop_integrations` accepts writes from
no client role at all.
"""

import uuid
from typing import Any

import anyio
import psycopg

from htco_application.etsy_connect import (
    ConnectedShop,
    EtsyShopConnectedElsewhereError,
    PendingAuthorization,
    ShopConnectionRequest,
)
from htco_application.shop_integrations import EtsyConnection
from htco_application.shops import ShopSummary
from htco_persistence.audit import append_audit
from htco_persistence.database import Database
from htco_persistence.shop_integrations import PostgresTokenVault
from htco_platform.crypto import Sealer, SealingError


def _verifier_context(state_hash: bytes) -> bytes:
    return b"private.oauth_authorizations\x00code_verifier\x00" + state_hash


class PostgresPendingAuthorizations:
    def __init__(self, database: Database, sealer: Sealer) -> None:
        self._database = database
        self._sealer = sealer

    async def save(self, pending: PendingAuthorization) -> None:
        sealed = self._sealer.seal(
            pending.code_verifier, context=_verifier_context(pending.state_hash)
        )

        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                # Housekeeping: the user's authorizations that can no longer be completed.
                connection.execute(
                    "delete from private.oauth_authorizations where user_id = %s "
                    "and expires_at <= %s",
                    [pending.user_id, pending.created_at],
                )
                connection.execute(
                    """
                    insert into private.oauth_authorizations
                      (id, state_hash, provider, organization_id, user_id, web_session_id,
                       code_verifier, scopes, redirect_uri, created_at, expires_at)
                    values (%s, %s, 'etsy', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        uuid.uuid7(),
                        pending.state_hash,
                        pending.organization_id,
                        pending.user_id,
                        pending.session_id,
                        sealed,
                        sorted(pending.scopes),
                        pending.redirect_uri,
                        pending.created_at,
                        pending.expires_at,
                    ],
                )

        await anyio.to_thread.run_sync(run)

    async def take(self, state_hash: bytes) -> PendingAuthorization | None:
        def run() -> tuple[Any, ...] | None:
            with self._database.connection() as connection:
                return connection.execute(
                    """
                    delete from private.oauth_authorizations where state_hash = %s
                    returning organization_id, user_id, web_session_id, code_verifier, scopes,
                              redirect_uri, created_at, expires_at
                    """,
                    [state_hash],
                ).fetchone()

        row = await anyio.to_thread.run_sync(run)
        if row is None:
            return None
        try:
            verifier = self._sealer.open(bytes(row[3]), context=_verifier_context(state_hash))
        except SealingError:  # another key, or an altered row: unusable, like an unknown state
            return None
        return PendingAuthorization(
            state_hash=state_hash,
            organization_id=row[0],
            user_id=row[1],
            session_id=row[2],
            code_verifier=verifier,
            scopes=frozenset(row[4]),
            redirect_uri=row[5],
            created_at=row[6],
            expires_at=row[7],
        )


class PostgresShopConnector:
    """Creates or updates one shop per Etsy shop and organization, in one transaction."""

    def __init__(self, database: Database, vault: PostgresTokenVault) -> None:
        self._database = database
        self._vault = vault

    async def connect(self, request: ShopConnectionRequest) -> ConnectedShop:
        return await anyio.to_thread.run_sync(self._connect, request)

    def _connect(self, request: ShopConnectionRequest) -> ConnectedShop:
        with self._database.connection() as db, db.transaction():
            # Two connections of the same Etsy shop (any organization) run one after the other.
            db.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"htco.etsy_connect:{request.etsy_shop_id}"],
            )
            elsewhere = db.execute(
                """
                select 1 from app.shop_integrations
                where provider = 'etsy' and etsy_shop_id = %s and organization_id <> %s
                  and status <> 'disconnected'
                limit 1
                """,
                [request.etsy_shop_id, request.organization_id],
            ).fetchone()
            if elsewhere is not None:
                raise EtsyShopConnectedElsewhereError(
                    f"Etsy shop {request.etsy_shop_id} is connected to another organization"
                )
            existing = db.execute(
                """
                select shop_id, scopes, status from app.shop_integrations
                where organization_id = %s and provider = 'etsy' and etsy_shop_id = %s
                for update
                """,
                [request.organization_id, request.etsy_shop_id],
            ).fetchone()
            shop = _upsert_shop(db, request, None if existing is None else existing[0])
            _grant_owner(db, request, shop.id)
            integration = self._vault.store_in(
                db,
                EtsyConnection(
                    organization_id=request.organization_id,
                    shop_id=shop.id,
                    etsy_shop_id=request.etsy_shop_id,
                    etsy_user_id=request.etsy_user_id,
                    scopes=request.scopes,
                    tokens=request.tokens,
                ),
            )
            before = (
                None if existing is None else {"scopes": sorted(existing[1]), "status": existing[2]}
            )
            append_audit(
                db,
                request.audit_record(shop.id, reconnected=existing is not None, before=before),
            )
        return ConnectedShop(shop=shop, integration=integration, reconnected=existing is not None)


def _upsert_shop(
    db: psycopg.Connection[Any], request: ShopConnectionRequest, shop_id: uuid.UUID | None
) -> ShopSummary:
    if shop_id is None:
        row = db.execute(
            """
            insert into app.shops (id, organization_id, name, marketplace, status)
            values (%s, %s, %s, 'etsy', 'active')
            returning id, organization_id, name, marketplace, status
            """,
            [uuid.uuid7(), request.organization_id, _shop_name(request.shop_name)],
        ).fetchone()
    else:
        row = db.execute(
            """
            update app.shops set name = %s, status = 'active'
            where id = %s and organization_id = %s
            returning id, organization_id, name, marketplace, status
            """,
            [_shop_name(request.shop_name), shop_id, request.organization_id],
        ).fetchone()
    if row is None:  # pragma: no cover - the integration's foreign key keeps the shop
        raise LookupError("the connected shop disappeared")
    return ShopSummary(
        id=row[0], organization_id=row[1], name=row[2], marketplace=row[3], status=row[4]
    )


def _grant_owner(
    db: psycopg.Connection[Any], request: ShopConnectionRequest, shop_id: uuid.UUID
) -> None:
    """Grant the connecting owner, and nobody else (AC-4.3.3). The Owner already reaches every
    shop through the role; the explicit grant keeps access if the role changes later."""
    membership = db.execute(
        """
        select id from app.memberships
        where organization_id = %s and user_id = %s and revoked_at is null and role = 'owner'
        """,
        [request.organization_id, request.owner_user_id],
    ).fetchone()
    if membership is None:
        raise PermissionError("the connecting user is not the organization's owner")
    db.execute(
        """
        insert into app.shop_access (id, organization_id, membership_id, shop_id)
        values (%s, %s, %s, %s)
        on conflict (membership_id, shop_id) do nothing
        """,
        [uuid.uuid7(), request.organization_id, membership[0], shop_id],
    )


def _shop_name(name: str) -> str:
    """Etsy shop names are short; the column allows 1-200 characters."""
    trimmed = name.strip()[:200]
    return trimmed or "Etsy shop"
