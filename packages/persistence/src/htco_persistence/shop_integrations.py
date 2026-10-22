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

The app key (replacing the Etsy app): each row keeps `app_key_fingerprint`, the fingerprint of
the keystring its tokens were issued for. With a configured fingerprint the vault treats a
connected row with another fingerprint as `needs_reauth` when it reads it (no Etsy call is made
with tokens of another app), and `retire_other_app_keys` (run at startup) stores that status with
an audit record. A row without a fingerprint (connected before it was recorded) takes the current
one at its first successful call or refresh: that proves its tokens work with this key.
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
from htco_application.shop_capabilities import ProbeSupersededError
from htco_application.shop_integrations import (
    EtsyConnection,
    IntegrationSummary,
    OAuthTokens,
    ShopIntegration,
    TokenVaultError,
)
from htco_domain.actors import ActorKind, ActorRef
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
disconnected_at, purge_after, connection_generation, app_key_fingerprint
"""

_log = structlog.get_logger(__name__)


def _context(shop_id: uuid.UUID, provider: IntegrationProvider = _PROVIDER) -> bytes:
    return b"app.shop_integrations\x00" + provider.value.encode("ascii") + b"\x00" + shop_id.bytes


class PostgresTokenVault:
    def __init__(
        self,
        database: Database,
        sealer: EnvelopeSealer,
        *,
        app_key_fingerprint: str | None = None,
    ) -> None:
        self._database = database
        self._sealer = sealer
        self._app_key = app_key_fingerprint

    async def retire_other_app_keys(self) -> int:
        """Mark every connected row whose tokens were issued for another app key `needs_reauth`,
        with one audit record each, in one transaction. Returns how many rows changed."""
        current = self._app_key
        if current is None:
            return 0

        def run() -> int:
            with self._database.connection() as connection, connection.transaction():
                rows = connection.execute(
                    """
                    update app.shop_integrations set status = 'needs_reauth'
                    where provider = %s and status = 'connected'
                      and app_key_fingerprint is not null and app_key_fingerprint <> %s
                    returning organization_id, shop_id
                    """,
                    [_PROVIDER.value, current],
                ).fetchall()
                for organization_id, shop_id in rows:
                    append_audit(
                        connection,
                        AuditRecord.new(
                            organization_id=organization_id,
                            actor=ActorRef(ActorKind.SYSTEM, "etsy-app-key-check"),
                            action="shop.integration_needs_reauth",
                            entity_type="shop",
                            entity_id=str(shop_id),
                            after={"reason": "app_key_changed", "provider": _PROVIDER.value},
                        ),
                    )
            return len(rows)

        retired = await anyio.to_thread.run_sync(run)
        if retired:
            _log.info("token_vault.app_key_changed", retired=retired)
        return retired

    async def store(self, connection: EtsyConnection) -> ShopIntegration:
        return await anyio.to_thread.run_sync(self._store, connection)

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        return await anyio.to_thread.run_sync(self._load, shop_id)

    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        """Like `load`, without opening the tokens: works even when they cannot be opened."""
        return await anyio.to_thread.run_sync(self._load, shop_id, False)

    async def update_tokens(
        self, shop_id: uuid.UUID, tokens: OAuthTokens, *, generation: int | None = None
    ) -> bool:
        return await anyio.to_thread.run_sync(self._update_tokens, shop_id, tokens, generation)

    async def record_refresh_failure(
        self, shop_id: uuid.UUID, *, at: datetime | None = None, generation: int | None = None
    ) -> int:
        def run() -> int:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    update app.shop_integrations
                    set failure_count = failure_count + 1,
                        last_refresh_failed_at = coalesce(%s, now())
                    where shop_id = %s and provider = %s and status = 'connected'
                      and (%s::integer is null or connection_generation = %s::integer)
                    returning failure_count
                    """,
                    [at, shop_id, _PROVIDER.value, generation, generation],
                ).fetchone()
            return 0 if row is None else int(row[0])

        return await anyio.to_thread.run_sync(run)

    async def record_successful_call(
        self, shop_id: uuid.UUID, *, at: datetime, generation: int | None = None
    ) -> None:
        await self._execute(
            """
            update app.shop_integrations
            set last_successful_call_at = %s,
                app_key_fingerprint = coalesce(app_key_fingerprint, %s)
            where shop_id = %s and provider = %s and status = 'connected'
              and (%s::integer is null or connection_generation = %s::integer)
            """,
            [at, self._app_key, shop_id, _PROVIDER.value, generation, generation],
        )

    async def mark_needs_reauth(
        self,
        shop_id: uuid.UUID,
        *,
        audit: AuditRecord | None = None,
        generation: int | None = None,
    ) -> bool:
        def run() -> bool:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    update app.shop_integrations set status = 'needs_reauth'
                    where shop_id = %s and provider = %s and status = 'connected'
                      and (%s::integer is null or connection_generation = %s::integer)
                    returning id
                    """,
                    [shop_id, _PROVIDER.value, generation, generation],
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
               access_expires_at, refresh_expires_at, status, failure_count, connection_generation,
               app_key_fingerprint)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'connected', 0, 1, %s)
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
              connection_generation = app.shop_integrations.connection_generation + 1,
              app_key_fingerprint = excluded.app_key_fingerprint
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
                self._app_key,
            ],
        ).fetchone()
        if row is None:  # the conflict update was skipped: another organization's shop
            raise TokenVaultError.wrong_organization(connection.shop_id)
        # A new connection starts without the previous one's probe results (Codex review of
        # #49): they may describe other scopes or permissions. The connect probe adds new ones.
        db.execute("delete from app.shop_capabilities where shop_id = %s", [connection.shop_id])
        return self._row(row)

    def _load(self, shop_id: uuid.UUID, open_tokens: bool = True) -> ShopIntegration | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                select {_COLUMNS} from app.shop_integrations
                where shop_id = %s and provider = %s
                """,  # noqa: S608 - constant column list
                [shop_id, _PROVIDER.value],
            ).fetchone()
        return None if row is None else self._row(row, open_tokens=open_tokens)

    def _update_tokens(
        self, shop_id: uuid.UUID, tokens: OAuthTokens, generation: int | None = None
    ) -> bool:
        envelope = self._seal(shop_id, tokens)
        access, refresh = envelope.ciphertexts
        with self._database.connection() as connection:
            cursor = connection.execute(
                """
                update app.shop_integrations
                set key_id = %s, wrapped_data_key = %s, access_token_ciphertext = %s,
                    refresh_token_ciphertext = %s, access_expires_at = %s,
                    refresh_expires_at = %s, failure_count = 0, last_refresh_failed_at = null,
                    app_key_fingerprint = coalesce(app_key_fingerprint, %s)
                where shop_id = %s and provider = %s and status = 'connected'
                  and (%s::integer is null or connection_generation = %s::integer)
                """,
                [
                    envelope.key_id,
                    envelope.wrapped_key,
                    access,
                    refresh,
                    tokens.access_expires_at,
                    tokens.refresh_expires_at,
                    self._app_key,
                    shop_id,
                    _PROVIDER.value,
                    generation,
                    generation,
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

    def _row(self, row: tuple[Any, ...], *, open_tokens: bool = True) -> ShopIntegration:
        shop_id: uuid.UUID = row[2]
        provider = IntegrationProvider(row[3])
        key_id: str | None = row[6]
        tokens = None
        if key_id is not None and open_tokens:
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
        fingerprint: str | None = row[22]
        return ShopIntegration(
            id=row[0],
            organization_id=row[1],
            shop_id=shop_id,
            provider=provider,
            etsy_shop_id=int(row[4]),
            etsy_user_id=int(row[5]),
            scopes=frozenset(row[10]),
            status=effective_status(IntegrationStatus(row[13]), fingerprint, self._app_key),
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
            app_key_fingerprint=fingerprint,
            refresh_expires_at=row[12],
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


def effective_status(
    status: IntegrationStatus, fingerprint: str | None, configured: str | None
) -> IntegrationStatus:
    """`needs_reauth` for a connected row whose tokens were issued for another app key."""
    if (
        status is IntegrationStatus.CONNECTED
        and configured is not None
        and fingerprint is not None
        and fingerprint != configured
    ):
        return IntegrationStatus.NEEDS_REAUTH
    return status


def _advisory(connection: psycopg.Connection[Any], function: str, key: str) -> None:
    connection.execute(
        f"select {function}(hashtextextended(%s, 0))",
        [key],
    )
    connection.commit()


# --- The shop card's view (slice 4.4) ------------------------------------------------------------

# Runs as the caller: the policy `shop_integrations_select` limits rows to Owner and Admin, and
# the sealed columns are not even granted to `authenticated`.
# One statement, so the connection and its probe results (slice 4.5, `shop_capabilities_select`)
# come from one snapshot: a reconnect between two reads cannot mix them (Codex review of #49).
_LIST_INTEGRATIONS = """
select i.shop_id, i.organization_id, i.provider, i.status, i.etsy_shop_id, i.scopes,
       i.access_expires_at, i.refresh_expires_at, i.failure_count, i.last_successful_call_at,
       i.created_at, i.updated_at, i.disconnected_at, i.purge_after, i.app_key_fingerprint,
       (select coalesce(json_agg(json_build_array(c.capability, c.state, c.detail, c.checked_at)),
                        '[]'::json)
        from app.shop_capabilities c where c.shop_id = i.shop_id) as capabilities
from app.shop_integrations i
where i.organization_id = %s
order by i.shop_id
"""


class PostgresIntegrationReader:
    def __init__(self, database: Database, *, app_key_fingerprint: str | None = None) -> None:
        self._database = database
        self._app_key = app_key_fingerprint

    async def list_integrations(
        self, claims: Mapping[str, Any], organization_id: uuid.UUID
    ) -> Sequence[IntegrationSummary]:
        def run() -> list[IntegrationSummary]:
            with self._database.user_transaction(claims) as connection:
                rows = connection.execute(_LIST_INTEGRATIONS, [organization_id]).fetchall()
            return [
                IntegrationSummary(
                    shop_id=row[0],
                    organization_id=row[1],
                    provider=IntegrationProvider(row[2]),
                    status=effective_status(IntegrationStatus(row[3]), row[14], self._app_key),
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
                    capabilities=_capabilities(row[15]),
                )
                for row in rows
            ]

        return await anyio.to_thread.run_sync(run)


def _capabilities(rows: Sequence[Sequence[Any]]) -> tuple[CapabilityResult, ...]:
    """A shop's probe results from the aggregated JSON, in `ShopCapability` order."""
    found: dict[ShopCapability, CapabilityResult] = {}
    for capability, state, detail, checked_at in rows:
        result = CapabilityResult(
            ShopCapability(capability),
            CapabilityState(state),
            detail,
            datetime.fromisoformat(checked_at),
        )
        found[result.capability] = result
    return tuple(found[c] for c in ShopCapability if c in found)


# --- Capability probe results (slice 4.5) --------------------------------------------------------


class PostgresCapabilityStore:
    """The latest probe result per shop and capability, written with the API's login role."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(  # noqa: PLR0913 - the results and what commits with them
        self,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        results: Sequence[CapabilityResult],
        *,
        generation: int,
        successful_call_at: datetime | None = None,
        audit: AuditRecord | None = None,
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
                # A probe a newer one overtook (same connection) leaves no trace either.
                newest = connection.execute(
                    "select max(checked_at) from app.shop_capabilities where shop_id = %s",
                    [shop_id],
                ).fetchone()
                started = min((r.checked_at for r in results), default=None)
                if (
                    newest is not None
                    and newest[0] is not None
                    and started is not None
                    and newest[0] > started
                ):
                    return False
                if successful_call_at is not None:
                    connection.execute(
                        """
                        update app.shop_integrations set last_successful_call_at = %s
                        where shop_id = %s and provider = %s
                        """,
                        [successful_call_at, shop_id, _PROVIDER.value],
                    )
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
                if audit is not None:
                    append_audit(connection, audit)
            return True

        return await anyio.to_thread.run_sync(run)

    async def reserve_check(
        self, shop_id: uuid.UUID, *, now: datetime, cooldown: timedelta, generation: int
    ) -> timedelta | None:
        """One conditional update: the row lock makes concurrent claims (any process) serial."""

        def run() -> timedelta | None:
            with self._database.connection() as connection, connection.transaction():
                claimed = connection.execute(
                    """
                    update app.shop_integrations set capability_check_reserved_at = %s
                    where shop_id = %s and provider = %s
                      and status = 'connected' and connection_generation = %s
                      and (capability_check_reserved_at is null
                           or capability_check_reserved_at <= %s)
                    returning id
                    """,
                    [now, shop_id, _PROVIDER.value, generation, now - cooldown],
                ).fetchone()
                if claimed is not None:
                    return None
                row = connection.execute(
                    """
                    select capability_check_reserved_at, status, connection_generation
                    from app.shop_integrations
                    where shop_id = %s and provider = %s
                    """,
                    [shop_id, _PROVIDER.value],
                ).fetchone()
            # Bound to the loaded connection: a replaced one makes no Etsy call (Codex, #49).
            if row is None or row[1] != "connected" or row[2] != generation:
                raise ProbeSupersededError(f"shop {shop_id} changed before it was probed")
            if row[0] is None:  # pragma: no cover - the update above would have claimed it
                return None
            reserved_at: datetime = row[0]
            return max(cooldown - (now - reserved_at), timedelta(0))

        return await anyio.to_thread.run_sync(run)
