"""Connecting an Etsy shop with OAuth 2.0 and PKCE (slice 4.3, ADR-0008).

1. `start_connection` (the route has checked Owner, MFA and a recent step-up): a random `state`
   (256 bits) and PKCE verifier are created; only the state's SHA-256 and the verifier (sealed by
   the adapter) are kept, bound to the organization, the user and the web session, for ten
   minutes. The answer is Etsy's authorization URL.
2. Etsy sends the browser to the web app's callback route, which posts `code` and `state` here as
   a same-site request (the session cookie comes along). `complete_connection` takes the pending
   authorization out of the store (single use, whatever happens next), checks that it belongs to
   this user and web session and has not expired, re-checks that the caller is still the Owner
   with MFA, exchanges the code with the verifier, looks up the user's shop, and connects it in
   one transaction (`ShopConnector`): one shop per Etsy shop and organization, a grant for the
   connecting owner only, tokens in the vault, the scopes Etsy granted, an audit record.

Every rejected callback is audited (AC-4.3.1). Errors name no token, code or verifier.
"""

import base64
import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NoReturn, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, decide
from htco_application.audit import AuditRecord
from htco_application.marketplace import MarketplaceError, MarketplaceNotFoundError, MarketplaceShop
from htco_application.shop_integrations import OAuthTokens, ShopIntegration
from htco_application.shops import ShopSummary
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role

# Phases 4 and 5 read shops, receipts and listings; later phases add scopes by re-consent.
ETSY_CONNECT_SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})
AUTHORIZATION_LIFETIME = timedelta(minutes=10)
# Etsy refresh tokens live 90 days (developers.etsy.com, authentication; re-check on renewal).
REFRESH_TOKEN_LIFETIME = timedelta(days=90)
_STATE_BYTES = 32
_VERIFIER_BYTES = 64  # 86 base64url characters, inside RFC 7636's 43-128


# --- Values ------------------------------------------------------------------------------------


def state_hash(state: str) -> bytes:
    return hashlib.sha256(state.encode("utf-8")).digest()


def code_challenge(verifier: str) -> str:
    """PKCE S256: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PendingAuthorization:
    """An authorization started by a user's web session and not yet completed."""

    state_hash: bytes = field(repr=False)
    organization_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    code_verifier: str = field(repr=False)
    scopes: frozenset[str]
    redirect_uri: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class TokenGrant:
    """What Etsy's token endpoint answered. `scopes` is None when Etsy did not name them."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: timedelta
    scopes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        prefix, dot, _ = self.access_token.partition(".")
        if not dot or not (prefix.isascii() and prefix.isdigit()) or int(prefix) <= 0:
            raise ValueError("the access token does not start with the Etsy user ID")
        if not self.refresh_token.strip():
            raise ValueError("the refresh token must not be blank")
        if self.expires_in <= timedelta(0):
            raise ValueError("the access token lifetime must be positive")

    @property
    def user_id(self) -> int:
        """Etsy prefixes access tokens with the numeric ID of the user who granted them."""
        return int(self.access_token.partition(".")[0])

    def tokens(self, now: datetime, *, refresh_expires_at: datetime | None = None) -> OAuthTokens:
        return OAuthTokens(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            access_expires_at=now + self.expires_in,
            refresh_expires_at=refresh_expires_at or now + REFRESH_TOKEN_LIFETIME,
        )


@dataclass(frozen=True)
class ShopConnectionRequest:
    """Everything one connection stores, in one transaction."""

    organization_id: uuid.UUID
    owner_user_id: uuid.UUID
    etsy_shop_id: int
    etsy_user_id: int
    shop_name: str
    scopes: frozenset[str]
    tokens: OAuthTokens = field(repr=False)
    correlation_id: str | None = None

    def audit_record(
        self,
        shop_id: uuid.UUID,
        *,
        reconnected: bool,
        before: dict[str, object] | None = None,
    ) -> AuditRecord:
        return AuditRecord.new(
            organization_id=self.organization_id,
            actor=ActorRef(ActorKind.USER, str(self.owner_user_id)),
            action="shop.reconnected" if reconnected else "shop.connected",
            entity_type="shop",
            entity_id=str(shop_id),
            before=before,
            after={
                "provider": "etsy",
                "etsy_shop_id": self.etsy_shop_id,
                "name": self.shop_name,
                "scopes": sorted(self.scopes),
                "status": "connected",
            },
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True)
class ConnectedShop:
    shop: ShopSummary
    integration: ShopIntegration
    reconnected: bool


# --- Errors ------------------------------------------------------------------------------------


class OAuthInvalidGrantError(Exception):
    """Etsy answered `invalid_grant`: the code or refresh token is expired, used or revoked."""


class OAuthClientRejectedError(OAuthInvalidGrantError):
    """Etsy answered `invalid_client`: the app's key or secret is not accepted, or the refresh
    token was issued to another app (the key was replaced). Reconnecting is the only way out."""


class OAuthUnavailableError(Exception):
    """Etsy's token endpoint failed otherwise (no answer, 5xx, an unexpected payload)."""


class StateRejectedError(Exception):
    """The callback's state is missing, unknown (or already used), expired or not this session's."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the connection's state was rejected ({reason})")
        self.reason = reason


class ConnectNotAllowedError(Exception):
    """The caller is no longer the organization's Owner with a second factor."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"connecting a shop is not allowed ({outcome.value})")
        self.outcome = outcome


class EtsyAuthorizationRefusedError(Exception):
    """Etsy refused the authorization code (expired, already used or not ours)."""


class EtsyUnreachableError(Exception):
    """Etsy did not answer usefully; nothing was stored."""


class EtsyAccountHasNoShopError(Exception):
    """The Etsy account that approved the connection has no shop."""


class EtsyShopConnectedElsewhereError(Exception):
    """This Etsy shop is connected to another organization; disconnect it there first."""


# --- Ports -------------------------------------------------------------------------------------


class EtsyAuthorizationServer(Protocol):
    def authorization_url(
        self, *, state: str, code_challenge: str, scopes: frozenset[str], redirect_uri: str
    ) -> str: ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenGrant: ...

    async def refresh(self, refresh_token: str) -> TokenGrant: ...


class EtsyShopDirectory(Protocol):
    async def shop_of_user(self, user_id: int, *, access_token: str) -> MarketplaceShop:
        """The shop owned by an Etsy user, with a token not yet in the vault."""
        ...


class PendingAuthorizations(Protocol):
    async def save(self, pending: PendingAuthorization) -> None: ...

    async def take(self, state_hash: bytes) -> PendingAuthorization | None:
        """Remove and return the authorization for this state: each state is used once."""
        ...


class ShopConnector(Protocol):
    async def connect(self, request: ShopConnectionRequest) -> ConnectedShop:
        """Create or update the shop, grant the owner, store the tokens and audit, atomically.
        Raises `EtsyShopConnectedElsewhereError`."""
        ...


class AuditLog(Protocol):
    async def append(self, record: AuditRecord) -> None: ...


@dataclass(frozen=True)
class EtsyConnect:
    """The connect flow's collaborators, built by the composition root."""

    authorization_server: EtsyAuthorizationServer
    directory: EtsyShopDirectory
    pending: PendingAuthorizations
    connector: ShopConnector
    audit: AuditLog
    redirect_uri: str
    scopes: frozenset[str] = ETSY_CONNECT_SCOPES


# --- Use cases ---------------------------------------------------------------------------------


async def start_connection(  # noqa: PLR0913 - each keyword is one part of the binding
    etsy: EtsyConnect,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime,
    random_token: Callable[[int], str] = secrets.token_urlsafe,
) -> str:
    """Keep a new state and verifier for this session; return Etsy's authorization URL."""
    state = random_token(_STATE_BYTES)
    verifier = random_token(_VERIFIER_BYTES)
    await etsy.pending.save(
        PendingAuthorization(
            state_hash=state_hash(state),
            organization_id=organization_id,
            user_id=user_id,
            session_id=session_id,
            code_verifier=verifier,
            scopes=etsy.scopes,
            redirect_uri=etsy.redirect_uri,
            created_at=now,
            expires_at=now + AUTHORIZATION_LIFETIME,
        )
    )
    return etsy.authorization_server.authorization_url(
        state=state,
        code_challenge=code_challenge(verifier),
        scopes=etsy.scopes,
        redirect_uri=etsy.redirect_uri,
    )


async def complete_connection(  # noqa: PLR0913 - the callback's inputs and its context
    etsy: EtsyConnect,
    auth: AuthContext,
    *,
    session_id: uuid.UUID,
    code: str,
    state: str | None,
    now: datetime,
    correlation_id: str | None,
) -> ConnectedShop:
    pending = await _validated(
        etsy, auth, session_id=session_id, state=state, now=now, correlation_id=correlation_id
    )
    _check_still_owner(auth, pending.organization_id, now)
    try:
        grant = await etsy.authorization_server.exchange_code(
            code=code, code_verifier=pending.code_verifier, redirect_uri=pending.redirect_uri
        )
    except OAuthInvalidGrantError:
        await _audit_rejection(etsy, auth, pending.organization_id, "code_refused", correlation_id)
        raise EtsyAuthorizationRefusedError("Etsy refused the authorization code") from None
    except OAuthUnavailableError:
        raise EtsyUnreachableError("Etsy's token endpoint did not answer usefully") from None
    try:
        shop = await etsy.directory.shop_of_user(grant.user_id, access_token=grant.access_token)
    except MarketplaceNotFoundError:
        raise EtsyAccountHasNoShopError("the Etsy account has no shop") from None
    except MarketplaceError:
        raise EtsyUnreachableError("Etsy did not answer the shop lookup") from None
    return await etsy.connector.connect(
        ShopConnectionRequest(
            organization_id=pending.organization_id,
            owner_user_id=auth.user_id,
            etsy_shop_id=shop.shop_id,
            etsy_user_id=grant.user_id,
            shop_name=shop.name,
            scopes=grant.scopes if grant.scopes is not None else pending.scopes,
            tokens=grant.tokens(now),
            correlation_id=correlation_id,
        )
    )


async def _validated(  # noqa: PLR0913 - the callback's inputs and its context
    etsy: EtsyConnect,
    auth: AuthContext,
    *,
    session_id: uuid.UUID,
    state: str | None,
    now: datetime,
    correlation_id: str | None,
) -> PendingAuthorization:
    if state is None or not state.strip():
        await _reject_everywhere(etsy, auth, "missing", correlation_id)
    pending = await etsy.pending.take(state_hash(state or ""))
    if pending is None:
        await _reject_everywhere(etsy, auth, "unknown", correlation_id)
    if pending.user_id != auth.user_id or pending.session_id != session_id:
        await _audit_rejection(etsy, auth, pending.organization_id, "foreign", correlation_id)
        raise StateRejectedError("foreign")
    if now >= pending.expires_at:
        await _audit_rejection(etsy, auth, pending.organization_id, "expired", correlation_id)
        raise StateRejectedError("expired")
    return pending


def _check_still_owner(auth: AuthContext, organization_id: uuid.UUID, now: datetime) -> None:
    """Owner with MFA, as at the start. The step-up was proven when the state was issued, at
    most ten minutes ago, so a step-up that lapsed since then does not stop the callback."""
    try:
        principal = auth.principal_for(organization_id)
    except NoMembershipError:
        raise ConnectNotAllowedError(Outcome.DENY) from None
    outcome = decide(principal, Capability.CONNECT_SHOP, shop=None, now=now).outcome
    if outcome not in {Outcome.ALLOW, Outcome.STEP_UP_REQUIRED}:
        raise ConnectNotAllowedError(outcome)


async def _reject_everywhere(
    etsy: EtsyConnect, auth: AuthContext, reason: str, correlation_id: str | None
) -> NoReturn:
    """A state that names no organization is audited in every organization the caller owns."""
    for membership in auth.memberships:
        if membership.role is Role.OWNER:
            await _audit_rejection(etsy, auth, membership.organization_id, reason, correlation_id)
    raise StateRejectedError(reason)


async def _audit_rejection(
    etsy: EtsyConnect,
    auth: AuthContext,
    organization_id: uuid.UUID,
    reason: str,
    correlation_id: str | None,
) -> None:
    await etsy.audit.append(
        AuditRecord.new(
            organization_id=organization_id,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            action="shop.connect_rejected",
            entity_type="etsy_authorization",
            entity_id=correlation_id or str(uuid.uuid7()),
            after={"reason": reason, "provider": "etsy"},
            correlation_id=correlation_id,
        )
    )
