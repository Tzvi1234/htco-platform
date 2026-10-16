"""The token vault (slice 4.2): Etsy tokens in `app.shop_integrations`, sealed by envelope
encryption.

Runs with the API's login role: the sealed columns are granted to no client role. Each write seals
the two tokens with a fresh data key, wrapped by the key ring's current key; the context binds the
envelope to the row's shop and provider, so an envelope moved to another row does not open.

Rotation (AC-4.2.2) re-wraps data keys in small batches, each in its own transaction with the rows
locked, so a refresh writing the same row waits instead of being overwritten. The ring still holds
the previous keys, so every row stays readable while rotation runs. Rows sealed with a key ID the
ring does not know are left alone (they cannot be opened anyway); `key_usage` shows them.

Nothing here logs a token or a key; errors name the shop only (AC-4.2.3).
"""

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import anyio
import psycopg
import structlog

from htco_application.audit import AuditRecord
from htco_application.shop_integrations import (
    EtsyConnection,
    IntegrationSummary,
    OAuthTokens,
    ShopIntegration,
    TokenVaultError,
)
from htco_domain.integrations import (
    CapabilityResult,
    CapabilityState,
    IntegrationProvider,
    IntegrationStatus,
    ShopCapability,
)
from htco_persistence.audit import append_audit
from htco_persistence.database import Database
from htco_platform.crypto import Envelope, EnvelopeSealer, SealingError

_PROVIDER = IntegrationProvider.ETSY
_COLUMNS = """
id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at, refresh_expires_at,
status, failure_count, last_successful_call_at, created_at, updated_at, last_refresh_failed_at,
disconnected_at, purge_after, connection_generation
"""

_log = structlog.get_logger(__name__)


def _context(shop_id: uuid.UUID, provider: IntegrationProvider = _PROVIDER) -> bytes:
    return b"app.shop_integrations\x00" + provider.value.encode("ascii") + b"\x00" + shop_id.bytes


class PostgresTokenVault:
    def __init__(self, database: Database, sealer: EnvelopeSealer) -> None:
        self._database = database
        self._sealer = sealer

    async def store(self, connection: EtsyConnection) -> ShopIntegration:
        return await anyio.to_thread.run_sync(self._store, connection)

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        return await anyio.to_thread.run_sync(self._load, shop_id)

    async def update_tokens(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> bool:
        return await anyio.to_thread.run_sync(self._update_tokens, shop_id, tokens)

    async def record_refresh_failure(
        self, shop_id: uuid.UUID, *, at: datetime | None = None
    ) -> int:
        def run() -> int:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    update app.shop_integrations
                    set failure_count = failure_count + 1,
                        last_refresh_failed_at = coalesce(%s, now())
                    where shop_id = %s and provider = %s and status = 'connected'
                    returning failure_count
                    """,
                    [at, shop_id, _PROVIDER.value],
                ).fetchone()
            return 0 if row is None else int(row[0])

        return await anyio.to_thread.run_sync(run)

    async def record_successful_call(self, shop_id: uuid.UUID, *, at: datetime) -> None:
        await self._execute(
            """
            update app.shop_integrations set last_successful_call_at = %s
            where shop_id = %s and provider = %s and status = 'connected'
            """,
            [at, shop_id, _PROVIDER.value],
        )

    async def mark_needs_reauth(
        self, shop_id: uuid.UUID, *, audit: AuditRecord | None = None
    ) -> bool:
        def run() -> bool:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    update app.shop_integrations set status = 'needs_reauth'
                    where shop_id = %s and provider = %s and status = 'connected'
                    returning id
                    """,
                    [shop_id, _PROVIDER.value],
                ).fetchone()
                if row is not None and audit is not None:
                    append_audit(connection, audit)
            return row is not None

        return await anyio.to_thread.run_sync(run)

    async def find_by_marketplace_shop(self, etsy_shop_id: int) -> ShopIntegration | None:
        def run() -> ShopIntegration | None:
            with self._database.connection() as connection:
                rows = connection.execute(
                    f"""
                    select {_COLUMNS} from app.shop_integrations
                    where etsy_shop_id = %s and provider = %s and status <> 'disconnected'
                    limit 2
                    """,  # noqa: S608 - constant column list
                    [etsy_shop_id, _PROVIDER.value],
                ).fetchall()
            if len(rows) > 1:  # the connect flow refuses this; never guess between tenants
                raise TokenVaultError(
                    f"Etsy shop {etsy_shop_id} is connected in more than one organization"
                )
            return self._row(rows[0]) if rows else None

        return await anyio.to_thread.run_sync(run)

    async def disconnect(
        self,
        shop_id: uuid.UUID,
        *,
        purge_after: datetime | None = None,
        audit: AuditRecord | None = None,
    ) -> bool:
        """Delete the tokens, mark the integration and the shop disconnected, keep the purge date
        and write `audit`, in one transaction. False when it was already disconnected."""

        def run() -> bool:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    update app.shop_integrations
                    set status = 'disconnected', key_id = null, wrapped_data_key = null,
                        access_token_ciphertext = null, refresh_token_ciphertext = null,
                        access_expires_at = null, refresh_expires_at = null,
                        disconnected_at = now(), purge_after = %s,
                        connection_generation = connection_generation + 1
                    where shop_id = %s and provider = %s and status <> 'disconnected'
                    returning organization_id
                    """,
                    [purge_after, shop_id, _PROVIDER.value],
                ).fetchone()
                if row is None:
                    return False
                connection.execute(
                    """
                    update app.shops set status = 'disconnected'
                    where id = %s and organization_id = %s
                    """,
                    [shop_id, row[0]],
                )
                # Probe results describe a connection that no longer exists.
                connection.execute(
                    "delete from app.shop_capabilities where shop_id = %s", [shop_id]
                )
                if audit is not None:
                    append_audit(connection, audit)
            return True

        return await anyio.to_thread.run_sync(run)

    async def rotate_keys(self, *, batch_size: int) -> int:
        """Re-wrap batch after batch until no row on a previous key is left; returns the count."""
        if batch_size < 1:
            raise ValueError("the batch size must be at least 1")
        total = 0
        while rotated := await self.rotate_batch(limit=batch_size):
            total += rotated
        _log.info(
            "token_vault.rotated",
            rotated=total,
            current_key_id=self._sealer.current_key_id,
            previous_key_ids=list(self._sealer.previous_key_ids),
        )
        return total

    async def rotate_batch(self, *, limit: int) -> int:
        """Re-wrap up to `limit` rows still on a previous key, in one transaction."""
        return await anyio.to_thread.run_sync(self._rotate_batch, limit)

    async def key_usage(self) -> dict[str, int]:
        def run() -> dict[str, int]:
            with self._database.connection() as connection:
                rows = connection.execute(
                    """
                    select key_id, count(*) from app.shop_integrations
                    where key_id is not null group by key_id order by key_id
                    """
                ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

        return await anyio.to_thread.run_sync(run)

    # Sync work in a worker thread ----------------------------------------------------------------

    def _seal(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> Envelope:
        return self._sealer.seal(
            [tokens.access_token, tokens.refresh_token], context=_context(shop_id)
        )

    def _store(self, connection: EtsyConnection) -> ShopIntegration:
        with self._database.connection() as db:
            return self.store_in(db, connection)

    def store_in(self, db: psycopg.Connection[Any], connection: EtsyConnection) -> ShopIntegration:
        """`store` inside the caller's transaction (the connect flow's unit of work)."""
        envelope = self._seal(connection.shop_id, connection.tokens)
        access, refresh = envelope.ciphertexts
        row = db.execute(
            f"""
            insert into app.shop_integrations
              (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id,
               wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext, scopes,
               access_expires_at, refresh_expires_at, status, failure_count, connection_generation)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'connected', 0, 1)
            on conflict (shop_id, provider) do update set
              etsy_shop_id = excluded.etsy_shop_id,
              etsy_user_id = excluded.etsy_user_id,
              key_id = excluded.key_id,
              wrapped_data_key = excluded.wrapped_data_key,
              access_token_ciphertext = excluded.access_token_ciphertext,
              refresh_token_ciphertext = excluded.refresh_token_ciphertext,
              scopes = excluded.scopes,
              access_expires_at = excluded.access_expires_at,
              refresh_expires_at = excluded.refresh_expires_at,
              status = 'connected',
              failure_count = 0,
              last_refresh_failed_at = null,
              disconnected_at = null,
              purge_after = null,
              connection_generation = app.shop_integrations.connection_generation + 1
            -- Never across tenants: a shop's row stays with its organization.
            where app.shop_integrations.organization_id = excluded.organization_id
            returning {_COLUMNS}
            """,  # noqa: S608 - constant column list
            [
                uuid.uuid7(),
                connection.organization_id,
                connection.shop_id,
                _PROVIDER.value,
                connection.etsy_shop_id,
                connection.etsy_user_id,
                envelope.key_id,
                envelope.wrapped_key,
                access,
                refresh,
                sorted(connection.scopes),
                connection.tokens.access_expires_at,
                connection.tokens.refresh_expires_at,
            ],
        ).fetchone()
        if row is None:  # the conflict update was skipped: another organization's shop
            raise TokenVaultError.wrong_organization(connection.shop_id)
        # A new connection starts without the previous one's probe results (Codex review of
        # #49): they may describe other scopes or permissions. The connect probe adds new ones.
        db.execute("delete from app.shop_capabilities where shop_id = %s", [connection.shop_id])
        return self._row(row)

    def _load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                select {_COLUMNS} from app.shop_integrations
                where shop_id = %s and provider = %s
                """,  # noqa: S608 - constant column list
                [shop_id, _PROVIDER.value],
            ).fetchone()
        return None if row is None else self._row(row)

    def _update_tokens(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> bool:
        envelope = self._seal(shop_id, tokens)
        access, refresh = envelope.ciphertexts
        with self._database.connection() as connection:
            cursor = connection.execute(
                """
                update app.shop_integrations
                set key_id = %s, wrapped_data_key = %s, access_token_ciphertext = %s,
                    refresh_token_ciphertext = %s, access_expires_at = %s,
                    refresh_expires_at = %s, failure_count = 0, last_refresh_failed_at = null
                where shop_id = %s and provider = %s and status = 'connected'
                """,
                [
                    envelope.key_id,
                    envelope.wrapped_key,
                    access,
                    refresh,
                    tokens.access_expires_at,
                    tokens.refresh_expires_at,
                    shop_id,
                    _PROVIDER.value,
                ],
            )
            return cursor.rowcount == 1

    def _rotate_batch(self, limit: int) -> int:
        previous = list(self._sealer.previous_key_ids)
        if not previous:
            return 0
        with self._database.connection() as connection, connection.transaction():
            rows = connection.execute(
                """
                select id, shop_id, provider, key_id, wrapped_data_key
                from app.shop_integrations
                where key_id = any(%s)
                order by id
                limit %s
                for update
                """,
                [previous, limit],
            ).fetchall()
            for row_id, shop_id, provider, key_id, wrapped in rows:
                envelope = Envelope(key_id=key_id, wrapped_key=bytes(wrapped), ciphertexts=())
                try:
                    rewrapped = self._sealer.rewrap(
                        envelope, context=_context(shop_id, IntegrationProvider(provider))
                    )
                except SealingError:
                    raise TokenVaultError.unreadable(shop_id) from None
                connection.execute(
                    """
                    update app.shop_integrations set key_id = %s, wrapped_data_key = %s
                    where id = %s
                    """,
                    [rewrapped.key_id, rewrapped.wrapped_key, row_id],
                )
        return len(rows)

    def _row(self, row: tuple[Any, ...]) -> ShopIntegration:
        shop_id: uuid.UUID = row[2]
        provider = IntegrationProvider(row[3])
        key_id: str | None = row[6]
        tokens = None
        if key_id is not None:
            envelope = Envelope(
                key_id=key_id,
                wrapped_key=bytes(row[7]),
                ciphertexts=(bytes(row[8]), bytes(row[9])),
            )
            try:
                access, refresh = self._sealer.open(envelope, context=_context(shop_id, provider))
            except SealingError:
                raise TokenVaultError.unreadable(shop_id) from None
            tokens = OAuthTokens(
                access_token=access,
                refresh_token=refresh,
                access_expires_at=row[11],
                refresh_expires_at=row[12],
            )
        return ShopIntegration(
            id=row[0],
            organization_id=row[1],
            shop_id=shop_id,
            provider=provider,
            etsy_shop_id=int(row[4]),
            etsy_user_id=int(row[5]),
            scopes=frozenset(row[10]),
            status=IntegrationStatus(row[13]),
            tokens=tokens,
            key_id=key_id,
            failure_count=int(row[14]),
            last_successful_call_at=row[15],
            created_at=row[16],
            updated_at=row[17],
            last_refresh_failed_at=row[18],
            disconnected_at=row[19],
            purge_after=row[20],
            generation=int(row[21]),
        )

    async def _execute(self, sql: str, params: list[object]) -> None:
        def run() -> None:
            with self._database.connection() as connection:
                connection.execute(sql, params)

        await anyio.to_thread.run_sync(run)


# --- The refresh lock (slice 4.4) ----------------------------------------------------------------


class PostgresRefreshLock:
    """A session-level advisory lock per shop, held on one pooled connection.

    Every API process (and the worker, from Phase 5) refreshes a shop's token only while holding
    it, so concurrent refreshes across processes become one (AC-4.4.1). If the process dies, the
    connection closes and Postgres releases the lock.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def hold(self, shop_id: uuid.UUID) -> AsyncIterator[None]:
        key = f"htco.etsy_token_refresh:{shop_id}"
        pooled = self._database.connection()
        connection = await anyio.to_thread.run_sync(pooled.__enter__)
        try:
            await anyio.to_thread.run_sync(_advisory, connection, "pg_advisory_lock", key)
            try:
                yield
            finally:
                await anyio.to_thread.run_sync(_advisory, connection, "pg_advisory_unlock", key)
        except BaseException as error:
            await anyio.to_thread.run_sync(pooled.__exit__, type(error), error, error.__traceback__)
            raise
        else:
            await anyio.to_thread.run_sync(pooled.__exit__, None, None, None)


def _advisory(connection: psycopg.Connection[Any], function: str, key: str) -> None:
    connection.execute(
        f"select {function}(hashtextextended(%s, 0))",
        [key],
    )
    connection.commit()


# --- The shop card's view (slice 4.4) ------------------------------------------------------------

# Runs as the caller: the policy `shop_integrations_select` limits rows to Owner and Admin, and
# the sealed columns are not even granted to `authenticated`.
_LIST_INTEGRATIONS = """
select shop_id, organization_id, provider, status, etsy_shop_id, scopes, access_expires_at,
       refresh_expires_at, failure_count, last_successful_call_at, created_at, updated_at,
       disconnected_at, purge_after
from app.shop_integrations
where organization_id = %s
order by shop_id
"""
# The latest probe results (slice 4.5), also read as the caller (`shop_capabilities_select`).
_LIST_CAPABILITIES = """
select shop_id, capability, state, detail, checked_at
from app.shop_capabilities
where organization_id = %s
"""


class PostgresIntegrationReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_integrations(
        self, claims: Mapping[str, Any], organization_id: uuid.UUID
    ) -> Sequence[IntegrationSummary]:
        def run() -> list[IntegrationSummary]:
            with self._database.user_transaction(claims) as connection:
                rows = connection.execute(_LIST_INTEGRATIONS, [organization_id]).fetchall()
                probed = connection.execute(_LIST_CAPABILITIES, [organization_id]).fetchall()
            capabilities = _capabilities_by_shop(probed)
            return [
                IntegrationSummary(
                    shop_id=row[0],
                    organization_id=row[1],
                    provider=IntegrationProvider(row[2]),
                    status=IntegrationStatus(row[3]),
                    etsy_shop_id=int(row[4]),
                    scopes=frozenset(row[5]),
                    access_expires_at=row[6],
                    refresh_expires_at=row[7],
                    failure_count=int(row[8]),
                    last_successful_call_at=row[9],
                    created_at=row[10],
                    updated_at=row[11],
                    disconnected_at=row[12],
                    purge_after=row[13],
                    capabilities=capabilities.get(row[0], ()),
                )
                for row in rows
            ]

        return await anyio.to_thread.run_sync(run)


def _capabilities_by_shop(
    rows: Sequence[tuple[Any, ...]],
) -> dict[uuid.UUID, tuple[CapabilityResult, ...]]:
    found: dict[uuid.UUID, dict[ShopCapability, CapabilityResult]] = {}
    for shop_id, capability, state, detail, checked_at in rows:
        result = CapabilityResult(
            ShopCapability(capability), CapabilityState(state), detail, checked_at
        )
        found.setdefault(shop_id, {})[result.capability] = result
    return {shop: tuple(by[c] for c in ShopCapability if c in by) for shop, by in found.items()}


# --- Capability probe results (slice 4.5) --------------------------------------------------------


class PostgresCapabilityStore:
    """The latest probe result per shop and capability, written with the API's login role."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(
        self,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        results: Sequence[CapabilityResult],
        *,
        generation: int,
    ) -> bool:
        def run() -> bool:
            with self._database.connection() as connection, connection.transaction():
                owned = connection.execute(
                    "select 1 from app.shops where id = %s and organization_id = %s",
                    [shop_id, organization_id],
                ).fetchone()
                if owned is None:
                    raise ValueError(
                        f"shop {shop_id} belongs to another organization; nothing was stored"
                    )
                # The integration row is locked like disconnect and reconnect lock it, so a probe
                # of an earlier connection can never store results after either (Codex, #49).
                current = connection.execute(
                    """
                    select 1 from app.shop_integrations
                    where shop_id = %s and provider = %s and status = 'connected'
                      and connection_generation = %s
                    for update
                    """,
                    [shop_id, _PROVIDER.value, generation],
                ).fetchone()
                if current is None:
                    return False
                for result in results:
                    # An older probe finishing late never replaces a newer result.
                    connection.execute(
                        """
                        insert into app.shop_capabilities
                          (id, organization_id, shop_id, capability, state, detail, checked_at)
                        values (%s, %s, %s, %s, %s, %s, %s)
                        on conflict (shop_id, capability) do update set
                          state = excluded.state,
                          detail = excluded.detail,
                          checked_at = excluded.checked_at
                        where app.shop_capabilities.organization_id = excluded.organization_id
                          and app.shop_capabilities.checked_at <= excluded.checked_at
                        """,
                        [
                            uuid.uuid7(),
                            organization_id,
                            shop_id,
                            result.capability.value,
                            result.state.value,
                            result.detail,
                            result.checked_at,
                        ],
                    )
            return True

        return await anyio.to_thread.run_sync(run)

    async def reserve_check(
        self, shop_id: uuid.UUID, *, now: datetime, cooldown: timedelta
    ) -> timedelta | None:
        """One conditional update: the row lock makes concurrent claims (any process) serial."""

        def run() -> timedelta | None:
            with self._database.connection() as connection, connection.transaction():
                claimed = connection.execute(
                    """
                    update app.shop_integrations set capability_check_reserved_at = %s
                    where shop_id = %s and provider = %s
                      and (capability_check_reserved_at is null
                           or capability_check_reserved_at <= %s)
                    returning id
                    """,
                    [now, shop_id, _PROVIDER.value, now - cooldown],
                ).fetchone()
                if claimed is not None:
                    return None
                row = connection.execute(
                    """
                    select capability_check_reserved_at from app.shop_integrations
                    where shop_id = %s and provider = %s
                    """,
                    [shop_id, _PROVIDER.value],
                ).fetchone()
            if row is None or row[0] is None:  # no such integration: nothing to wait for
                return None
            reserved_at: datetime = row[0]
            return max(cooldown - (now - reserved_at), timedelta(0))

        return await anyio.to_thread.run_sync(run)
