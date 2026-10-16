"""In-memory fakes for the Etsy connect flow and token refresh (slices 4.3 and 4.4).

Shared by the application and API tests. Token values are obviously fake but token-shaped (the
numeric Etsy user ID, a dot, then random text), so leak checks can search for them.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import anyio

from htco_application.audit import AuditRecord
from htco_application.etsy_connect import (
    ConnectedShop,
    EtsyShopConnectedElsewhereError,
    OAuthInvalidGrantError,
    OAuthUnavailableError,
    PendingAuthorization,
    ShopConnectionRequest,
    TokenGrant,
)
from htco_application.marketplace import (
    Listing,
    ListingState,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplaceShop,
    Page,
    ReceiptAddressCheck,
)
from htco_application.shop_integrations import (
    EtsyConnection,
    OAuthTokens,
    ShopIntegration,
)
from htco_application.shops import ShopSummary
from htco_domain.integrations import (
    CapabilityResult,
    IntegrationProvider,
    IntegrationStatus,
    ShopCapability,
)

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
ETSY_USER = 12345678
ETSY_SHOP = 87654321
REDIRECT_URI = "https://dash.ht-co.app/integrations/etsy/callback"
GOOD_CODE = "good-authorization-code"


def grant(n: int, *, scopes: frozenset[str] | None = None, user: int = ETSY_USER) -> TokenGrant:
    marker = uuid.UUID(int=n).hex
    return TokenGrant(
        access_token=f"{user}.fake-access-{marker}",
        refresh_token=f"{user}.fake-refresh-{marker}",
        expires_in=timedelta(hours=1),
        scopes=scopes,
    )


@dataclass
class AuthorizationServer:
    """Etsy's OAuth endpoints: the authorization URL, code exchange and refresh."""

    scopes: frozenset[str] | None = None
    exchanged: list[tuple[str, str, str]] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    issued: int = 0
    # What the next refreshes do: "ok", "invalid_grant" or "unavailable".
    refresh_outcome: str = "ok"
    exchange_outcome: str = "ok"
    # Slows the refresh down so concurrent callers overlap.
    refresh_delay: float = 0.0

    def authorization_url(
        self, *, state: str, code_challenge: str, scopes: frozenset[str], redirect_uri: str
    ) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": "fake-keystring",
                "redirect_uri": redirect_uri,
                "scope": " ".join(sorted(scopes)),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"https://www.etsy.com/oauth/connect?{query}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenGrant:
        self.exchanged.append((code, code_verifier, redirect_uri))
        if self.exchange_outcome == "unavailable":
            raise OAuthUnavailableError("Etsy did not answer the token request")
        if code != GOOD_CODE or self.exchange_outcome == "invalid_grant":
            raise OAuthInvalidGrantError("Etsy refused the authorization code")
        self.issued += 1
        return grant(self.issued, scopes=self.scopes)

    async def refresh(self, refresh_token: str) -> TokenGrant:
        self.refreshed.append(refresh_token)
        if self.refresh_delay:
            await anyio.sleep(self.refresh_delay)
        if self.refresh_outcome == "invalid_grant":
            raise OAuthInvalidGrantError("Etsy refused the refresh token")
        if self.refresh_outcome == "unavailable":
            raise OAuthUnavailableError("Etsy did not answer the token request")
        self.issued += 1
        return grant(100 + self.issued)


def etsy_shop(shop_id: int = ETSY_SHOP, name: str = "FakeCraftsShop") -> MarketplaceShop:
    return MarketplaceShop(
        shop_id=shop_id,
        name=name,
        currency_code="USD",
        url=None,
        is_vacation=False,
        active_listing_count=3,
        sold_transaction_count=10,
        created_at=NOW - timedelta(days=400),
        updated_at=NOW - timedelta(days=1),
    )


@dataclass
class Directory:
    """Etsy's getShopByOwnerUserId."""

    shops: dict[int, MarketplaceShop] = field(default_factory=lambda: {ETSY_USER: etsy_shop()})
    asked: list[tuple[int, str]] = field(default_factory=list)

    async def shop_of_user(self, user_id: int, *, access_token: str) -> MarketplaceShop:
        self.asked.append((user_id, access_token))
        if user_id not in self.shops:
            raise MarketplaceNotFoundError("Etsy answered 404", status=404)
        return self.shops[user_id]


@dataclass
class Pending:
    """Pending authorizations by state hash; `take` removes (single use)."""

    rows: dict[bytes, PendingAuthorization] = field(default_factory=dict)

    async def save(self, pending: PendingAuthorization) -> None:
        self.rows[pending.state_hash] = pending

    async def take(self, state_hash: bytes) -> PendingAuthorization | None:
        return self.rows.pop(state_hash, None)


@dataclass
class AuditLog:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


@dataclass
class Connector:
    """Shops and integrations in memory: one shop per Etsy shop and organization."""

    audit: AuditLog
    shops: dict[uuid.UUID, ShopSummary] = field(default_factory=dict)
    integrations: dict[uuid.UUID, ShopIntegration] = field(default_factory=dict)
    grants: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    requests: list[ShopConnectionRequest] = field(default_factory=list)
    # When set, connected integrations are also stored here (the vault behind the probes).
    vault: Vault | None = None

    async def connect(self, request: ShopConnectionRequest) -> ConnectedShop:
        self.requests.append(request)
        for integration in self.integrations.values():
            if (
                integration.etsy_shop_id == request.etsy_shop_id
                and integration.organization_id != request.organization_id
                and integration.status is not IntegrationStatus.DISCONNECTED
            ):
                raise EtsyShopConnectedElsewhereError("connected in another organization")
        existing = next(
            (
                i
                for i in self.integrations.values()
                if i.organization_id == request.organization_id
                and i.etsy_shop_id == request.etsy_shop_id
            ),
            None,
        )
        shop_id = existing.shop_id if existing is not None else uuid.uuid7()
        shop = ShopSummary(shop_id, request.organization_id, request.shop_name, "etsy", "active")
        self.shops[shop_id] = shop
        if (shop_id, request.owner_user_id) not in self.grants:
            self.grants.append((shop_id, request.owner_user_id))
        connection = EtsyConnection(
            organization_id=request.organization_id,
            shop_id=shop_id,
            etsy_shop_id=request.etsy_shop_id,
            etsy_user_id=request.etsy_user_id,
            scopes=request.scopes,
            tokens=request.tokens,
        )
        integration = stored_integration(connection)
        self.integrations[shop_id] = integration
        if self.vault is not None:
            self.vault.rows[shop_id] = integration
        await self.audit.append(request.audit_record(shop_id, reconnected=existing is not None))
        return ConnectedShop(shop=shop, integration=integration, reconnected=existing is not None)


def stored_integration(connection: EtsyConnection, *, at: datetime = NOW) -> ShopIntegration:
    return ShopIntegration(
        id=uuid.uuid7(),
        organization_id=connection.organization_id,
        shop_id=connection.shop_id,
        provider=IntegrationProvider.ETSY,
        etsy_shop_id=connection.etsy_shop_id,
        etsy_user_id=connection.etsy_user_id,
        scopes=connection.scopes,
        status=IntegrationStatus.CONNECTED,
        tokens=connection.tokens,
        key_id="fake-key",
        failure_count=0,
        last_successful_call_at=None,
        created_at=at,
        updated_at=at,
    )


@dataclass
class Vault:
    """The token vault's refresh-related methods, in memory."""

    rows: dict[uuid.UUID, ShopIntegration] = field(default_factory=dict)
    audit: list[AuditRecord] = field(default_factory=list)
    updates: list[OAuthTokens] = field(default_factory=list)

    def add(self, connection: EtsyConnection) -> ShopIntegration:
        stored = stored_integration(connection)
        self.rows[connection.shop_id] = stored
        return stored

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        return self.rows.get(shop_id)

    async def find_by_marketplace_shop(self, etsy_shop_id: int) -> ShopIntegration | None:
        return next(
            (
                row
                for row in self.rows.values()
                if row.etsy_shop_id == etsy_shop_id
                and row.status is not IntegrationStatus.DISCONNECTED
            ),
            None,
        )

    async def update_tokens(self, shop_id: uuid.UUID, tokens: OAuthTokens) -> bool:
        row = self.rows[shop_id]
        if row.status is not IntegrationStatus.CONNECTED:
            return False
        self.updates.append(tokens)
        self.rows[shop_id] = replace(
            row, tokens=tokens, failure_count=0, last_refresh_failed_at=None
        )
        return True

    async def record_refresh_failure(
        self, shop_id: uuid.UUID, *, at: datetime | None = None
    ) -> int:
        row = self.rows[shop_id]
        if row.status is not IntegrationStatus.CONNECTED:
            return 0
        self.rows[shop_id] = replace(
            row, failure_count=row.failure_count + 1, last_refresh_failed_at=at or NOW
        )
        return row.failure_count + 1

    async def mark_needs_reauth(
        self, shop_id: uuid.UUID, *, audit: AuditRecord | None = None
    ) -> bool:
        row = self.rows[shop_id]
        if row.status is not IntegrationStatus.CONNECTED:
            return False
        self.rows[shop_id] = replace(row, status=IntegrationStatus.NEEDS_REAUTH)
        if audit is not None:
            self.audit.append(audit)
        return True

    async def record_successful_call(self, shop_id: uuid.UUID, *, at: datetime) -> None:
        row = self.rows[shop_id]
        if row.status is IntegrationStatus.CONNECTED:
            self.rows[shop_id] = replace(row, last_successful_call_at=at)

    async def disconnect(
        self,
        shop_id: uuid.UUID,
        *,
        purge_after: datetime | None = None,
        audit: AuditRecord | None = None,
    ) -> bool:
        row = self.rows[shop_id]
        if row.status is IntegrationStatus.DISCONNECTED:
            return False
        self.rows[shop_id] = replace(
            row,
            status=IntegrationStatus.DISCONNECTED,
            tokens=None,
            key_id=None,
            disconnected_at=NOW if purge_after is None else purge_after - timedelta(days=30),
            purge_after=purge_after,
        )
        if audit is not None:
            self.audit.append(audit)
        return True


@dataclass
class Capabilities:
    """The capability store in memory: the latest result per shop and capability."""

    rows: dict[uuid.UUID, dict[ShopCapability, CapabilityResult]] = field(default_factory=dict)
    saved: list[tuple[uuid.UUID, uuid.UUID, tuple[CapabilityResult, ...]]] = field(
        default_factory=list
    )
    # When each shop's last re-check was claimed.
    reserved: dict[uuid.UUID, datetime] = field(default_factory=dict)
    # The connection generation each save named.
    generations: list[int] = field(default_factory=list)

    async def save(
        self,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        results: Sequence[CapabilityResult],
        *,
        generation: int,
    ) -> bool:
        self.saved.append((organization_id, shop_id, tuple(results)))
        self.generations.append(generation)
        current = self.rows.setdefault(shop_id, {})
        for result in results:
            current[result.capability] = result
        return True

    async def reserve_check(
        self, shop_id: uuid.UUID, *, now: datetime, cooldown: timedelta
    ) -> timedelta | None:
        last = self.reserved.get(shop_id)
        if last is not None and now - last < cooldown:
            return cooldown - (now - last)
        self.reserved[shop_id] = now
        return None


@dataclass
class Probe:
    """The marketplace's read calls the capability probes make; each answer or error scripted."""

    shop: MarketplaceShop | MarketplaceError = field(default_factory=etsy_shop)
    addresses: ReceiptAddressCheck | MarketplaceError = field(
        default_factory=lambda: ReceiptAddressCheck(receipts_seen=1, address_present=True)
    )
    listings: Page[Listing] | MarketplaceError = field(
        default_factory=lambda: Page(items=(), total_count=4, offset=0)
    )
    calls: list[str] = field(default_factory=list)

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop:
        self.calls.append(f"get_shop:{shop_id}:{urgent}")
        if isinstance(self.shop, MarketplaceError):
            raise self.shop
        return self.shop

    async def check_receipt_addresses(
        self, shop_id: int, *, urgent: bool = True
    ) -> ReceiptAddressCheck:
        self.calls.append(f"check_receipt_addresses:{shop_id}:{urgent}")
        if isinstance(self.addresses, MarketplaceError):
            raise self.addresses
        return self.addresses

    async def list_listings(
        self,
        shop_id: int,
        *,
        state: ListingState = ListingState.ACTIVE,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Listing]:
        self.calls.append(f"list_listings:{shop_id}:{state.value}:{limit}:{offset}:{urgent}")
        if isinstance(self.listings, MarketplaceError):
            raise self.listings
        return self.listings


class Lock:
    """A per-shop lock in memory; counts how often it was taken."""

    def __init__(self) -> None:
        self._locks: dict[uuid.UUID, anyio.Lock] = {}
        self.taken = 0

    @asynccontextmanager
    async def hold(self, shop_id: uuid.UUID) -> AsyncIterator[None]:
        lock = self._locks.setdefault(shop_id, anyio.Lock())
        async with lock:
            self.taken += 1
            yield
