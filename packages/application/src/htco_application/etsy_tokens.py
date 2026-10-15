"""Etsy access tokens from the vault, refreshed on demand (slice 4.4, ADR-0008).

`VaultAccessTokenProvider` is the `AccessTokenProvider` behind the Etsy adapter. A stored token
with more than `REFRESH_MARGIN` left is returned as it is. Otherwise the provider takes the shop's
refresh lock (one in-process lock per shop, then the database's advisory lock across processes),
reads the row again, since another caller may have refreshed it meanwhile, and refreshes once:
ten concurrent requests on an expired token cause one refresh call (AC-4.4.1). The newest tokens
are stored every time, including the new refresh token.

Failures never loop (AC-4.4.2): `invalid_grant` (a revoked or expired refresh token) marks the
integration `needs_reauth` at once; any other failure is counted, and the third in a row marks it
`needs_reauth`. Both are audited once. While the old access token still works, a failed refresh
returns it; the next call tries again. Errors and audit records never carry a token.
"""

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.audit import AuditRecord
from htco_application.etsy_connect import (
    EtsyAuthorizationServer,
    OAuthInvalidGrantError,
    OAuthUnavailableError,
)
from htco_application.marketplace import MarketplaceAuthError, MarketplaceUnavailableError
from htco_application.shop_integrations import OAuthTokens, ShopIntegration
from htco_domain.actors import ActorKind, ActorRef

REFRESH_MARGIN = timedelta(minutes=5)
MAX_REFRESH_FAILURES = 3
_ACTOR = ActorRef(ActorKind.SYSTEM, "etsy-token-refresh")


class RefreshableTokens(Protocol):
    """The part of the token vault (`TokenVault`) the provider uses."""

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...

    async def find_by_marketplace_shop(self, etsy_shop_id: int) -> ShopIntegration | None: ...

    async def update_tokens(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> bool: ...

    async def record_refresh_failure(self, shop_id: uuid.UUID) -> int: ...

    async def mark_needs_reauth(
        self, shop_id: uuid.UUID, *, audit: AuditRecord | None = None
    ) -> bool: ...


class RefreshLock(Protocol):
    def hold(self, shop_id: uuid.UUID) -> AbstractAsyncContextManager[None]:
        """Exclusive per shop across every API process while the context is open."""
        ...


class VaultAccessTokenProvider:
    def __init__(
        self,
        vault: RefreshableTokens,
        authorization_server: EtsyAuthorizationServer,
        lock: RefreshLock,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._vault = vault
        self._server = authorization_server
        self._lock = lock
        self._clock = clock
        self._local: dict[uuid.UUID, asyncio.Lock] = {}

    async def access_token(self, shop_id: int) -> str:
        found = await self._vault.find_by_marketplace_shop(shop_id)
        if found is None:
            raise MarketplaceAuthError(f"Etsy shop {shop_id} is not connected")
        tokens = _usable(found, shop_id)
        if self._fresh(tokens):
            return tokens.access_token
        local = self._local.setdefault(found.shop_id, asyncio.Lock())
        async with local, self._lock.hold(found.shop_id):
            current = await self._vault.load(found.shop_id)
            if current is None:
                raise MarketplaceAuthError(f"Etsy shop {shop_id} is not connected")
            tokens = _usable(current, shop_id)
            if self._fresh(tokens):  # refreshed by another caller while we waited
                return tokens.access_token
            return await self._refresh(current, tokens, shop_id)

    def _fresh(self, tokens: OAuthTokens) -> bool:
        return tokens.access_expires_at - self._clock() > REFRESH_MARGIN

    async def _refresh(self, integration: ShopIntegration, tokens: OAuthTokens, etsy: int) -> str:
        now = self._clock()
        try:
            grant = await self._server.refresh(tokens.refresh_token)
        except OAuthInvalidGrantError:
            await self._needs_reauth(integration, "invalid_grant")
            raise MarketplaceAuthError(
                f"Etsy refused the refresh token of shop {etsy}; reconnect the shop"
            ) from None
        except OAuthUnavailableError:
            failures = await self._vault.record_refresh_failure(integration.shop_id)
            if failures >= MAX_REFRESH_FAILURES:
                await self._needs_reauth(integration, "refresh_failed")
                raise MarketplaceAuthError(
                    f"refreshing the token of Etsy shop {etsy} failed {failures} times; reconnect"
                ) from None
            if tokens.access_expires_at > now:  # still valid for a little while
                return tokens.access_token
            raise MarketplaceUnavailableError(
                f"refreshing the token of Etsy shop {etsy} failed"
            ) from None
        # A new refresh token starts a new lifetime; the same one keeps its expiry.
        same_refresh = grant.refresh_token == tokens.refresh_token
        fresh = grant.tokens(
            now, refresh_expires_at=tokens.refresh_expires_at if same_refresh else None
        )
        if not await self._vault.update_tokens(integration.shop_id, fresh):
            raise MarketplaceAuthError(f"Etsy shop {etsy} was disconnected")
        return fresh.access_token

    async def _needs_reauth(self, integration: ShopIntegration, reason: str) -> None:
        await self._vault.mark_needs_reauth(
            integration.shop_id,
            audit=AuditRecord.new(
                organization_id=integration.organization_id,
                actor=_ACTOR,
                action="shop.integration_needs_reauth",
                entity_type="shop",
                entity_id=str(integration.shop_id),
                after={"reason": reason, "provider": integration.provider.value},
            ),
        )


def _usable(integration: ShopIntegration, etsy: int) -> OAuthTokens:
    if not integration.is_usable or integration.tokens is None:
        raise MarketplaceAuthError(
            f"Etsy shop {etsy} needs to be reconnected ({integration.status.value})"
        )
    return integration.tokens
