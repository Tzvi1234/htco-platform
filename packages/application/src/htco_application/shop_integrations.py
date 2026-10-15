"""The token vault port (slice 4.2): a shop's marketplace tokens, sealed at rest.

The vault keeps one integration per shop and provider. Tokens are encrypted before they reach the
database (envelope encryption with a key from the environment) and decrypted only when loaded;
they never appear in a repr, a string, a log line or an error message (AC-4.2.3). The OAuth
connect flow (slice 4.3) stores them, refresh (slice 4.4) updates them, disconnect (slice 4.7)
deletes them.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide
from htco_application.audit import AuditRecord
from htco_application.shops import OrganizationRequiredError, ShopAccessDeniedError
from htco_domain.integrations import (
    IntegrationProvider,
    IntegrationStatus,
    TokenHealth,
    token_health,
)


class TokenVaultError(Exception):
    """Stored tokens could not be opened: a key is missing or the row was altered.

    The message names the shop, never a token or a key.
    """

    @classmethod
    def unreadable(cls, shop_id: uuid.UUID) -> TokenVaultError:
        return cls(f"the stored tokens of shop {shop_id} cannot be opened (missing key or altered)")

    @classmethod
    def wrong_organization(cls, shop_id: uuid.UUID) -> TokenVaultError:
        return cls(f"shop {shop_id} belongs to another organization; nothing was stored")


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.access_token.strip() or not self.refresh_token.strip():
            raise ValueError("the access token and the refresh token must not be blank")


@dataclass(frozen=True)
class EtsyConnection:
    """What a completed OAuth connection stores for one shop."""

    organization_id: uuid.UUID
    shop_id: uuid.UUID
    etsy_shop_id: int
    etsy_user_id: int
    scopes: frozenset[str]
    tokens: OAuthTokens

    def __post_init__(self) -> None:
        if self.etsy_shop_id <= 0 or self.etsy_user_id <= 0:
            raise ValueError("the etsy shop and user IDs must be positive")
        if any(not scope.strip() for scope in self.scopes):
            raise ValueError("a granted scope must not be blank")


@dataclass(frozen=True)
class ShopIntegration:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    provider: IntegrationProvider
    etsy_shop_id: int
    etsy_user_id: int
    scopes: frozenset[str]
    status: IntegrationStatus
    # None once disconnected: the tokens are deleted.
    tokens: OAuthTokens | None
    # The ID of the key that wraps this row's data key (not secret); None once disconnected.
    key_id: str | None
    failure_count: int
    last_successful_call_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # When the last refresh attempt failed (None after a success); later attempts wait a while.
    last_refresh_failed_at: datetime | None = None

    @property
    def is_usable(self) -> bool:
        """Calls may be made with these tokens."""
        return self.status is IntegrationStatus.CONNECTED and self.tokens is not None


class TokenVault(Protocol):
    async def store(self, connection: EtsyConnection) -> ShopIntegration:
        """Connect or reconnect: one row per shop, status connected, failures reset."""
        ...

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        """The shop's integration with its tokens opened; raises `TokenVaultError`."""
        ...

    async def update_tokens(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> bool:
        """After a refresh: store the newest tokens. False when the shop is no longer connected,
        so a refresh that finishes after a disconnect never brings tokens back."""
        ...

    async def record_refresh_failure(
        self, shop_id: uuid.UUID, *, at: datetime | None = None
    ) -> int:
        """Count a failed refresh made `at` (default: the database's now); returns the failures
        since the last success."""
        ...

    async def record_successful_call(self, shop_id: uuid.UUID, *, at: datetime) -> None: ...

    async def mark_needs_reauth(
        self, shop_id: uuid.UUID, *, audit: AuditRecord | None = None
    ) -> bool:
        """Status `needs_reauth`, with `audit` written in the same transaction. False (and no
        record) when the shop was not connected, so repeated failures audit once."""
        ...

    async def find_by_marketplace_shop(self, etsy_shop_id: int) -> ShopIntegration | None:
        """The integration of a marketplace shop that is not disconnected (tokens opened)."""
        ...

    async def disconnect(self, shop_id: uuid.UUID) -> None:
        """Delete the tokens and keep the row, marked disconnected."""
        ...

    async def rotate_keys(self, *, batch_size: int) -> int:
        """Re-wrap every row sealed with a previous key under the current key; returns the count.
        Rows stay readable throughout (AC-4.2.2)."""
        ...

    async def key_usage(self) -> dict[str, int]:
        """How many rows each key ID wraps (key IDs are not secret)."""
        ...


@dataclass(frozen=True)
class IntegrationSummary:
    """What the shop card shows about a connection: never a token."""

    shop_id: uuid.UUID
    organization_id: uuid.UUID
    provider: IntegrationProvider
    status: IntegrationStatus
    etsy_shop_id: int
    scopes: frozenset[str]
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None
    failure_count: int
    last_successful_call_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, integration: ShopIntegration) -> IntegrationSummary:
        tokens = integration.tokens
        return cls(
            shop_id=integration.shop_id,
            organization_id=integration.organization_id,
            provider=integration.provider,
            status=integration.status,
            etsy_shop_id=integration.etsy_shop_id,
            scopes=integration.scopes,
            access_expires_at=None if tokens is None else tokens.access_expires_at,
            refresh_expires_at=None if tokens is None else tokens.refresh_expires_at,
            failure_count=integration.failure_count,
            last_successful_call_at=integration.last_successful_call_at,
            created_at=integration.created_at,
            updated_at=integration.updated_at,
        )

    def health(self, now: datetime) -> TokenHealth:
        return token_health(self.status, self.refresh_expires_at, now=now)


class IntegrationReader(Protocol):
    async def list_integrations(
        self, claims: Mapping[str, Any], organization_id: uuid.UUID
    ) -> Sequence[IntegrationSummary]:
        """The organization's integrations, read as the caller (RLS: Owner and Admin only)."""
        ...


async def list_integrations(
    auth: AuthContext,
    reader: IntegrationReader,
    *,
    now: datetime,
    organization_id: uuid.UUID | None = None,
) -> tuple[IntegrationSummary, ...]:
    """Owner and Admin (organization-wide roles) see every integration of the organization; other
    roles see shops, not their connections (PERMISSION_MATRIX: connection health is an admin
    concern). Raises `NoMembershipError`, `OrganizationRequiredError`, `ShopAccessDeniedError`."""
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)
    decision = decide(principal, Capability.VIEW_OVERVIEW, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW or decision.scope is not Scope.ORGANIZATION:
        raise ShopAccessDeniedError("only organization-wide roles see connections")
    found = await reader.list_integrations(auth.claims, organization)
    return tuple(sorted(found, key=lambda summary: summary.shop_id))
