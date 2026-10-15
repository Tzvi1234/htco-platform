"""Access tokens from the vault, refreshed on demand (slice 4.4).

A token with under five minutes left is refreshed under a per-shop lock, so concurrent callers
cause one refresh (AC-4.4.1; the database lock is proven in tests/integration). The newest refresh
token is always stored. A refused refresh token (`invalid_grant`) or three failed refreshes mark
the integration `needs_reauth`, audited, with no retry loop (AC-4.4.2).
"""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import anyio
import pytest

from htco_application.etsy_connect import TokenGrant
from htco_application.etsy_tokens import (
    MAX_REFRESH_FAILURES,
    REFRESH_MARGIN,
    REFRESH_RETRY_INTERVAL,
    VaultAccessTokenProvider,
)
from htco_application.marketplace import MarketplaceAuthError, MarketplaceUnavailableError
from htco_application.shop_integrations import EtsyConnection, OAuthTokens, ShopIntegration
from htco_domain.actors import ActorKind
from htco_domain.integrations import IntegrationStatus
from tests.etsy_fakes import ETSY_SHOP, NOW, AuthorizationServer, Lock, Vault, grant

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("a3000000-0000-0000-0000-000000000001")
OLD_ACCESS = "12345678.old-access-token-value"
OLD_REFRESH = "12345678.old-refresh-token-value"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self, *, access_left: timedelta) -> None:
        self.clock = Clock()
        self.vault = Vault()
        self.server = AuthorizationServer()
        self.lock = Lock()
        self.vault.add(
            EtsyConnection(
                organization_id=ORG,
                shop_id=SHOP,
                etsy_shop_id=ETSY_SHOP,
                etsy_user_id=12345678,
                scopes=frozenset({"shops_r"}),
                tokens=OAuthTokens(
                    access_token=OLD_ACCESS,
                    refresh_token=OLD_REFRESH,
                    access_expires_at=NOW + access_left,
                    refresh_expires_at=NOW + timedelta(days=30),
                ),
            )
        )
        self.provider = VaultAccessTokenProvider(
            self.vault, self.server, self.lock, clock=self.clock
        )

    @property
    def row(self) -> ShopIntegration:
        return self.vault.rows[SHOP]


async def test_access_token__more_than_five_minutes_left__stored_token_without_refresh() -> None:
    world = World(access_left=REFRESH_MARGIN + timedelta(seconds=1))

    token = await world.provider.access_token(ETSY_SHOP)

    assert token == OLD_ACCESS
    assert world.server.refreshed == []
    assert world.lock.taken == 0


@pytest.mark.parametrize("left", [REFRESH_MARGIN, timedelta(minutes=1), -timedelta(hours=2)])
async def test_access_token__under_five_minutes_left__refreshed_and_newest_tokens_stored(
    left: timedelta,
) -> None:
    world = World(access_left=left)

    token = await world.provider.access_token(ETSY_SHOP)

    assert world.server.refreshed == [OLD_REFRESH]
    assert token != OLD_ACCESS
    stored = world.row.tokens
    assert stored is not None
    assert stored.access_token == token
    assert stored.refresh_token != OLD_REFRESH  # the newest refresh token is kept
    assert stored.access_expires_at == NOW + timedelta(hours=1)
    assert stored.refresh_expires_at == NOW + timedelta(days=90)


async def test_access_token__etsy_returns_the_same_refresh_token__its_expiry_is_kept() -> None:
    world = World(access_left=timedelta(0))

    async def same_refresh(refresh_token: str) -> TokenGrant:
        world.server.refreshed.append(refresh_token)
        return replace(grant(7), refresh_token=refresh_token)

    world.server.refresh = same_refresh  # type: ignore[method-assign]

    await world.provider.access_token(ETSY_SHOP)

    stored = world.row.tokens
    assert stored is not None
    assert stored.refresh_token == OLD_REFRESH
    assert stored.refresh_expires_at == NOW + timedelta(days=30)


@pytest.mark.ac("AC-4.4.1")
async def test_access_token__ten_concurrent_requests_on_an_expired_token__one_refresh() -> None:
    world = World(access_left=-timedelta(minutes=1))
    world.server.refresh_delay = 0.01
    tokens: list[str] = []

    async def call() -> None:
        tokens.append(await world.provider.access_token(ETSY_SHOP))

    async with anyio.create_task_group() as group:
        for _ in range(10):
            group.start_soon(call)

    assert len(world.server.refreshed) == 1
    assert len(set(tokens)) == 1
    assert len(world.vault.updates) == 1


@pytest.mark.ac("AC-4.4.2")
async def test_access_token__refresh_token_revoked__needs_reauth_audited_without_retry() -> None:
    world = World(access_left=timedelta(0))
    world.server.refresh_outcome = "invalid_grant"

    with pytest.raises(MarketplaceAuthError):
        await world.provider.access_token(ETSY_SHOP)
    with pytest.raises(MarketplaceAuthError):
        await world.provider.access_token(ETSY_SHOP)  # no second refresh attempt

    assert len(world.server.refreshed) == 1
    assert world.row.status is IntegrationStatus.NEEDS_REAUTH
    [record] = world.vault.audit
    assert record.action == "shop.integration_needs_reauth"
    assert record.organization_id == ORG
    assert record.entity_id == str(SHOP)
    assert record.actor.kind is ActorKind.SYSTEM
    assert record.after == {"reason": "invalid_grant", "provider": "etsy"}
    assert OLD_REFRESH not in repr(record)


@pytest.mark.ac("AC-4.4.2")
async def test_access_token__three_failed_refreshes__needs_reauth_after_the_third() -> None:
    world = World(access_left=-timedelta(minutes=1))
    world.server.refresh_outcome = "unavailable"

    for attempt in range(1, MAX_REFRESH_FAILURES + 1):
        with pytest.raises(MarketplaceUnavailableError if attempt < 3 else MarketplaceAuthError):
            await world.provider.access_token(ETSY_SHOP)
        world.clock.now += REFRESH_RETRY_INTERVAL  # failed attempts are spaced out
        assert world.row.failure_count == attempt
        expected = IntegrationStatus.NEEDS_REAUTH if attempt == 3 else IntegrationStatus.CONNECTED
        assert world.row.status is expected

    with pytest.raises(MarketplaceAuthError):
        await world.provider.access_token(ETSY_SHOP)

    assert MAX_REFRESH_FAILURES == 3
    assert len(world.server.refreshed) == 3
    [record] = world.vault.audit
    assert record.after == {"reason": "refresh_failed", "provider": "etsy"}


@pytest.mark.ac("AC-4.4.2")
async def test_access_token__callers_waiting_during_one_failed_refresh__share_its_result() -> None:
    """One Etsy outage seen by many queued callers counts once, not once per caller."""
    world = World(access_left=-timedelta(minutes=1))
    world.server.refresh_outcome = "unavailable"
    world.server.refresh_delay = 0.01
    outcomes: list[str] = []

    async def call() -> None:
        try:
            await world.provider.access_token(ETSY_SHOP)
        except MarketplaceUnavailableError:
            outcomes.append("unavailable")

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(call)

    assert len(world.server.refreshed) == 1
    assert world.row.failure_count == 1
    assert world.row.status is IntegrationStatus.CONNECTED
    assert outcomes == ["unavailable"] * 5


async def test_access_token__after_a_failed_refresh__no_new_attempt_for_a_minute() -> None:
    world = World(access_left=timedelta(minutes=2))
    world.server.refresh_outcome = "unavailable"
    first = await world.provider.access_token(ETSY_SHOP)

    world.clock.now += REFRESH_RETRY_INTERVAL - timedelta(seconds=1)
    second = await world.provider.access_token(ETSY_SHOP)
    world.clock.now += timedelta(seconds=1)
    await world.provider.access_token(ETSY_SHOP)

    assert timedelta(minutes=1) == REFRESH_RETRY_INTERVAL
    assert first == second == OLD_ACCESS  # still valid, served without a new attempt
    assert len(world.server.refreshed) == 2
    assert world.row.failure_count == 2


async def test_access_token__expired_and_refresh_failed_recently__unavailable_without_attempt() -> (
    None
):
    world = World(access_left=-timedelta(minutes=1))
    world.server.refresh_outcome = "unavailable"
    with pytest.raises(MarketplaceUnavailableError):
        await world.provider.access_token(ETSY_SHOP)

    world.server.refresh_outcome = "ok"
    with pytest.raises(MarketplaceUnavailableError):
        await world.provider.access_token(ETSY_SHOP)
    world.clock.now += REFRESH_RETRY_INTERVAL
    token = await world.provider.access_token(ETSY_SHOP)

    assert len(world.server.refreshed) == 2
    assert token != OLD_ACCESS
    assert world.row.last_refresh_failed_at is None


async def test_access_token__refresh_failed_but_the_token_still_works__stored_token() -> None:
    world = World(access_left=timedelta(minutes=2))
    world.server.refresh_outcome = "unavailable"

    token = await world.provider.access_token(ETSY_SHOP)

    assert token == OLD_ACCESS
    assert world.row.failure_count == 1


async def test_access_token__a_success_resets_the_failure_count() -> None:
    world = World(access_left=-timedelta(minutes=1))
    world.server.refresh_outcome = "unavailable"
    with pytest.raises(MarketplaceUnavailableError):
        await world.provider.access_token(ETSY_SHOP)

    world.server.refresh_outcome = "ok"
    await world.provider.access_token(ETSY_SHOP)

    assert world.row.failure_count == 0


async def test_access_token__unknown_shop__auth_error_naming_no_token() -> None:
    world = World(access_left=timedelta(hours=1))

    with pytest.raises(MarketplaceAuthError) as raised:
        await world.provider.access_token(999)

    assert "999" in str(raised.value)


@pytest.mark.parametrize("status", [IntegrationStatus.NEEDS_REAUTH])
async def test_access_token__integration_not_connected__auth_error_without_refresh(
    status: IntegrationStatus,
) -> None:
    world = World(access_left=timedelta(0))
    world.vault.rows[SHOP] = replace(world.row, status=status)

    with pytest.raises(MarketplaceAuthError):
        await world.provider.access_token(ETSY_SHOP)

    assert world.server.refreshed == []


async def test_access_token__disconnected_while_refreshing__refreshed_tokens_are_not_stored() -> (
    None
):
    world = World(access_left=timedelta(0))
    original = world.server.refresh

    async def refresh_then_disconnect(refresh_token: str) -> TokenGrant:
        result = await original(refresh_token)
        world.vault.rows[SHOP] = replace(
            world.row, status=IntegrationStatus.DISCONNECTED, tokens=None
        )
        return result

    world.server.refresh = refresh_then_disconnect  # type: ignore[method-assign]

    with pytest.raises(MarketplaceAuthError):
        await world.provider.access_token(ETSY_SHOP)

    assert world.vault.updates == []


async def test_access_token__errors_never_carry_a_token() -> None:
    world = World(access_left=timedelta(0))
    world.server.refresh_outcome = "invalid_grant"

    with pytest.raises(MarketplaceAuthError) as raised:
        await world.provider.access_token(ETSY_SHOP)

    text = f"{raised.value!s} {raised.value!r}"
    assert OLD_ACCESS not in text
    assert OLD_REFRESH not in text
