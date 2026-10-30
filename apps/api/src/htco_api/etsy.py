"""Connecting an Etsy shop (slice 4.3) and the shop card's connection health (slice 4.4).

`POST /v1/shops/etsy/connect`: the organization's Owner, signed in with a second factor and
stepped up in the last ten minutes, from the web app (cookie session, CSRF header and origin). The
answer is Etsy's authorization URL; the browser goes there.

`GET /v1/integrations/etsy/callback` is the redirect URI registered at Etsy (Codex security review,
owner gate v4): the code and state Etsy sends back never reach the web app's URL, its JavaScript or
a referrer. Etsy's redirect is a cross-site navigation, which carries no `SameSite=Strict` session
cookie, so the callback only seals them into the short-lived `HttpOnly` cookie
`__Host-htco_etsy_callback` and sends the browser to the web app's clean
`/integrations/etsy/callback` page. That page then calls `POST /v1/integrations/etsy/callback`
without a body, as a same-site request with the session cookie and the CSRF header; the API
redeems the code from the sealed cookie. The state must belong to this browser session; a
rejected callback is audited. The callback is generic: any connect flow that sends the owner to
Etsy (a first connection, a reconnection, a re-consent for more scopes) finishes here.

`GET /v1/shop-integrations`: Owner and Admin see each connection's status, scopes, token health
and the latest capability probe results (slice 4.5), and Etsy's remaining daily request budget
with the floor below which routine calls are deferred (`etsy_quota`, AC-4.1.2).

`POST /v1/shop-integrations/{shop_id}/capability-checks`: Owner and Admin re-check what HTCO may do
with the shop. Read-only calls to Etsy; at most one re-check per shop every 30 seconds. The probes
also run right after a connection completes.

`POST /v1/shop-integrations/{shop_id}/disconnect`: the Owner, with MFA and a recent step-up,
deletes the shop's tokens; the shop is marked disconnected with a purge date 30 days ahead
(slice 4.7). Etsy offers no token revocation endpoint, so nothing is sent to Etsy.

No response carries a token, a code or a verifier (AC-4.2.3); connect answers are `no-store`.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from htco_api import notices
from htco_api.auth import CurrentAuth, web_session_id
from htco_api.identity import ORGANIZATION_REQUIRED, ShopView, problem_responses
from htco_api.policy_guard import (
    FORBIDDEN,
    MFA_REQUIRED,
    STEP_UP_REQUIRED,
    require_capability,
)
from htco_api.problems import (
    NOT_FOUND,
    NOT_READY,
    ProblemError,
    ProblemType,
    ShopNotConnectedProblem,
    ValidationProblem,
    request_id_of,
)
from htco_api.sealed_cookies import (
    ETSY_CALLBACK_COOKIE,
    CookieTooLargeError,
    expire,
    expired_header,
    open_sealed,
    set_sealed,
)
from htco_api.web_auth import WebAuth
from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome
from htco_application.etsy_connect import (
    ConnectedShop,
    ConnectNotAllowedError,
    EtsyAccountHasNoShopError,
    EtsyAuthorizationRefusedError,
    EtsyConnect,
    EtsyShopConnectedElsewhereError,
    EtsyUnreachableError,
    StateRejectedError,
    complete_connection,
    start_connection,
)
from htco_application.marketplace import QuotaMonitor
from htco_application.shop_capabilities import (
    CapabilityChecks,
    RecheckTooSoonError,
    ShopNotConnectedError,
    probe_capabilities,
    recheck_capabilities,
)
from htco_application.shop_dashboard import ShopDashboard
from htco_application.shop_integrations import (
    DisconnectingVault,
    DisconnectNotAllowedError,
    IntegrationReader,
    IntegrationSummary,
    RemoveNotAllowedError,
    ShopRemover,
    disconnect_shop,
    list_integrations,
    remove_shop,
)
from htco_application.shops import (
    OrganizationRequiredError,
    ShopAccessDeniedError,
    ShopNotFoundError,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import (
    CapabilityResult,
    CapabilityState,
    IntegrationProvider,
    IntegrationStatus,
    ShopCapability,
    TokenHealth,
)
from htco_domain.notifications import NotificationKind
from htco_platform.crypto import Sealer

ETSY_UNAVAILABLE = ProblemType(
    "etsy-unavailable",
    "Etsy connection unavailable",
    503,
    "Connecting an Etsy shop is not set up on this server yet.",
)
ETSY_STATE_REJECTED = ProblemType(
    "etsy-state-rejected",
    "Connection link not valid",
    400,
    "This Etsy connection is invalid, expired, already used or from another session. "
    "Start connecting again.",
)
ETSY_AUTHORIZATION_REFUSED = ProblemType(
    "etsy-authorization-refused",
    "Etsy refused the connection",
    400,
    "Etsy did not accept this approval. Start connecting again.",
)
ETSY_UNREACHABLE = ProblemType(
    "etsy-unreachable",
    "Etsy did not answer",
    502,
    "Etsy did not answer. Nothing was saved; try again shortly.",
)
ETSY_NO_SHOP = ProblemType(
    "etsy-no-shop",
    "No Etsy shop",
    409,
    "The Etsy account that approved the connection has no shop.",
)
ETSY_SHOP_CONNECTED_ELSEWHERE = ProblemType(
    "etsy-shop-connected-elsewhere",
    "Shop connected elsewhere",
    409,
    "This Etsy shop is connected to another organization. Disconnect it there first.",
)
WEB_SESSION_REQUIRED = ProblemType(
    "web-session-required",
    "Use the web app",
    403,
    "Connecting a shop works only from a signed-in HTCO web app session.",
)
SHOP_NOT_CONNECTED = ProblemType(
    "shop-not-connected",
    "Shop not connected",
    409,
    "This shop is not connected to Etsy right now. Reconnect it first.",
)


def not_connected(error: ShopNotConnectedError) -> ProblemError:
    """`shop-not-connected` with its reason, so the web app can offer to reconnect."""
    return ProblemError(SHOP_NOT_CONNECTED, extra={"reason": error.reason})


CAPABILITY_CHECK_TOO_SOON = ProblemType(
    "capability-check-too-soon",
    "Checked moments ago",
    429,
    "This shop was checked moments ago. Try again in a few seconds.",
)
_PROBLEM_FOR_OUTCOME = {
    Outcome.MFA_REQUIRED: MFA_REQUIRED,
    Outcome.STEP_UP_REQUIRED: STEP_UP_REQUIRED,
}
_log = structlog.get_logger(__name__)
# Connect and disconnect: the Owner, `aal2`, a step-up in the last ten minutes.
_Owner = Annotated[AuthContext, Depends(require_capability(Capability.CONNECT_SHOP))]


class AuthorizationView(BaseModel):
    # Send the browser here; it returns to the web app's callback route.
    authorization_url: str


# Etsy's code and state wait here between Etsy's redirect and the web app's finishing request.
ETSY_CALLBACK_LIFETIME = timedelta(minutes=10)
# The web app's finishing page, on the same origin as the API (ADR-0006); a clean URL.
ETSY_FINISH_PAGE = "/integrations/etsy/callback"


class CapabilityView(BaseModel):
    capability: ShopCapability
    # `available` only after a successful read-only probe; `unknown` when nothing proved either.
    state: CapabilityState
    # Why, as a short code (for example `read_ok`, `scope_missing`, `no_safe_probe`).
    detail: str
    checked_at: datetime


class CapabilityCheckView(BaseModel):
    shop_id: uuid.UUID
    # In display order; every capability is present.
    capabilities: list[CapabilityView]


class IntegrationView(BaseModel):
    shop_id: uuid.UUID
    provider: IntegrationProvider
    status: IntegrationStatus
    etsy_shop_id: int
    # Granted scopes, sorted.
    scopes: list[str]
    # `expiring`: the refresh token ends within 10 days; reconnect to keep calls working.
    token_health: TokenHealth
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None
    # Failed refreshes since the last success; `needs_reauth` after three.
    failure_count: int
    last_successful_call_at: datetime | None
    connected_at: datetime
    updated_at: datetime
    # Set while disconnected: when, and after which date the shop's data may be purged.
    disconnected_at: datetime | None = None
    purge_after: datetime | None = None
    # The latest probe results (empty until the first probe).
    capabilities: list[CapabilityView] = Field(default_factory=list)


class EtsyQuotaView(BaseModel):
    """The shop card's quota figure: this API process's last-seen value, which can be stale.

    Most Etsy calls run in the worker, so `remaining_today` and `below_floor` may be hours old or
    from the previous Etsy day, and `below_floor` ignores the snapshot's age. Show `observed_at`
    as "as of", never as a live count.
    """

    # Etsy's daily request budget is the app's, shared by every shop: as this server last saw it
    # (`x-limit-per-day`, `x-remaining-today`); nulls before its first Etsy call.
    limit_per_day: int | None
    remaining_today: int | None
    observed_at: datetime | None
    # Below this many requests left today, routine calls wait for the next day (urgent ones run).
    floor: int
    below_floor: bool


class IntegrationList(BaseModel):
    items: list[IntegrationView]
    # For the shop card (AC-4.1.2); null when Etsy is not set up on this server.
    etsy_quota: EtsyQuotaView | None = None


def _quota_view(request: Request) -> EtsyQuotaView | None:
    monitor: QuotaMonitor | None = getattr(request.app.state, "etsy_quota", None)
    if monitor is None:
        return None
    status = monitor.quota_status()
    snapshot = status.snapshot
    return EtsyQuotaView(
        limit_per_day=None if snapshot is None else snapshot.limit_per_day,
        remaining_today=None if snapshot is None else snapshot.remaining_today,
        observed_at=None if snapshot is None else snapshot.observed_at,
        floor=status.floor,
        below_floor=status.below_floor,
    )


class ConnectedShopView(BaseModel):
    shop: ShopView
    integration: IntegrationView
    # True when this Etsy shop was already connected: the same shop record was updated.
    reconnected: bool


def _view(summary: IntegrationSummary, now: datetime) -> IntegrationView:
    return IntegrationView(
        shop_id=summary.shop_id,
        provider=summary.provider,
        status=summary.status,
        etsy_shop_id=summary.etsy_shop_id,
        scopes=sorted(summary.scopes),
        token_health=summary.health(now),
        access_expires_at=summary.access_expires_at,
        refresh_expires_at=summary.refresh_expires_at,
        failure_count=summary.failure_count,
        last_successful_call_at=summary.last_successful_call_at,
        connected_at=summary.created_at,
        updated_at=summary.updated_at,
        disconnected_at=summary.disconnected_at,
        purge_after=summary.purge_after,
        capabilities=_capability_views(summary.capabilities),
    )


def _capability_views(results: tuple[CapabilityResult, ...]) -> list[CapabilityView]:
    return [
        CapabilityView(
            capability=r.capability, state=r.state, detail=r.detail, checked_at=r.checked_at
        )
        for r in results
    ]


def _connected_view(
    connected: ConnectedShop, now: datetime, capabilities: tuple[CapabilityResult, ...] = ()
) -> ConnectedShopView:
    return ConnectedShopView(
        shop=ShopView(**vars(connected.shop)),
        integration=_view(IntegrationSummary.of(connected.integration, capabilities), now),
        reconnected=connected.reconnected,
    )


def _checks(request: Request) -> CapabilityChecks:
    checks = getattr(request.app.state, "capability_checks", None)
    if not isinstance(checks, CapabilityChecks):
        raise ProblemError(ETSY_UNAVAILABLE)
    return checks


def _remover(request: Request) -> ShopRemover:
    remover = getattr(request.app.state, "shop_remover", None)
    if remover is None:
        raise ProblemError(ETSY_UNAVAILABLE)
    found: ShopRemover = remover
    return found


def _vault(request: Request) -> DisconnectingVault:
    vault = getattr(request.app.state, "token_vault", None)
    if vault is None:
        raise ProblemError(ETSY_UNAVAILABLE)
    found: DisconnectingVault = vault
    return found


async def _probe_after_connect(
    request: Request, connected: ConnectedShop, auth: AuthContext, now: datetime
) -> tuple[CapabilityResult, ...]:
    """Best effort: the connection is already saved; a failed probe leaves the card empty."""
    checks = getattr(request.app.state, "capability_checks", None)
    if not isinstance(checks, CapabilityChecks):
        return ()
    try:
        return await probe_capabilities(
            checks,
            connected.integration,
            now=now,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            trigger="connect",
            correlation_id=request_id_of(request),
        )
    except Exception as error:
        _log.warning("etsy.capability_probe_failed", error=type(error).__name__)
        return ()


def _etsy(request: Request) -> EtsyConnect:
    etsy = getattr(request.app.state, "etsy", None)
    if not isinstance(etsy, EtsyConnect):
        raise ProblemError(ETSY_UNAVAILABLE)
    return etsy


def _session(request: Request) -> uuid.UUID:
    session = web_session_id(request)
    if session is None:
        raise ProblemError(WEB_SESSION_REQUIRED)
    return session


def _callback_sealer(request: Request) -> Sealer:
    """The web sign-in's cookie sealer; the connect flow is off without web sign-in anyway."""
    web = getattr(request.app.state, "web_auth", None)
    if not isinstance(web, WebAuth) or web.link_sealer is None:
        raise ProblemError(ETSY_UNAVAILABLE)
    return web.link_sealer


def _to_finish(outcome: str | None = None) -> RedirectResponse:
    """A 303 to the web app's finishing page; `outcome` is the only query, never a secret."""
    location = ETSY_FINISH_PAGE if outcome is None else f"{ETSY_FINISH_PAGE}?outcome={outcome}"
    return RedirectResponse(
        location,
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _reader(request: Request) -> IntegrationReader:
    reader = getattr(request.app.state, "integrations", None)
    if reader is None:
        raise ProblemError(NOT_READY)
    found: IntegrationReader = reader
    return found


def etsy_router(*, clock: Callable[[], datetime]) -> APIRouter:  # noqa: PLR0915 - one nested function per route
    router = APIRouter(prefix="/v1", tags=["etsy"])

    @router.post(
        "/shops/etsy/connect",
        summary="Start connecting an Etsy shop",
        responses=problem_responses(
            {
                403: "Not the Owner, no second factor, no recent step-up "
                "(forbidden, mfa-required, step-up-required, web-session-required), or the "
                "request did not come from the web app (csrf-failed)",
                503: "Sign-in, the database or the Etsy connection is unavailable "
                "(etsy-unavailable)",
            }
        ),
    )
    async def connect_etsy_shop(
        request: Request, response: Response, auth: _Owner
    ) -> AuthorizationView:
        """Answers Etsy's authorization URL (PKCE S256, a single-use state bound to this
        session for ten minutes). Requested scopes: shops_r, transactions_r, listings_r."""
        etsy = _etsy(request)
        session = _session(request)
        organization = auth.default_organization()
        if organization is None:  # pragma: no cover - require_capability refuses this first
            raise ProblemError(FORBIDDEN)
        url = await start_connection(
            etsy,
            organization_id=organization,
            user_id=auth.user_id,
            session_id=session,
            now=clock(),
        )
        response.headers["Cache-Control"] = "no-store"
        return AuthorizationView(authorization_url=url)

    @router.get(
        "/integrations/etsy/callback",
        status_code=status.HTTP_303_SEE_OTHER,
        response_class=RedirectResponse,
        summary="Etsy's redirect after consent",
        responses={
            303: {
                "description": "To the web app's `/integrations/etsy/callback` page with a clean "
                "URL; `?outcome=cancelled` when the owner declined at Etsy, `?outcome=failed` "
                "when Etsy reported another error, `?outcome=incomplete` without a code"
            },
            503: {"description": "The Etsy connection is unavailable (etsy-unavailable)"},
        },
    )
    async def etsy_redirect(
        request: Request,
        code: Annotated[str | None, Query(max_length=2048)] = None,
        state: Annotated[str | None, Query(max_length=512)] = None,
        error: Annotated[str | None, Query(max_length=256)] = None,
    ) -> RedirectResponse:
        """Seals Etsy's code and state into the `__Host-htco_etsy_callback` cookie (`HttpOnly`,
        `Secure`, `SameSite=Strict`, ten minutes) and sends the browser on; nothing is redeemed
        yet, since this cross-site redirect carries no session cookie."""
        _etsy(request)
        sealer = _callback_sealer(request)
        if error is not None:
            return _to_finish("cancelled" if error == "access_denied" else "failed")
        if not code:
            return _to_finish("incomplete")
        redirect = _to_finish()
        try:
            set_sealed(
                redirect,
                sealer,
                ETSY_CALLBACK_COOKIE,
                {"code": code} | ({} if state is None else {"state": state}),
                now=clock(),
                lifetime=ETSY_CALLBACK_LIFETIME,
            )
        except CookieTooLargeError:
            return _to_finish("failed")
        return redirect

    @router.post(
        "/integrations/etsy/callback",
        summary="Finish connecting an Etsy shop",
        responses=problem_responses(
            {
                400: "No callback cookie, or its state is missing, unknown, used, expired or from "
                "another session "
                "(etsy-state-rejected), Etsy refused the code (etsy-authorization-refused), "
                "or the body is not valid JSON",
                403: "Not the Owner with a second factor (forbidden, mfa-required), not a web "
                "session (web-session-required), or not from the web app (csrf-failed)",
                409: "The Etsy account has no shop (etsy-no-shop), or the shop is connected to "
                "another organization (etsy-shop-connected-elsewhere)",
                502: "Etsy did not answer usefully; nothing was saved (etsy-unreachable)",
                503: "Sign-in, the database or the Etsy connection is unavailable "
                "(etsy-unavailable)",
            }
        ),
    )
    async def complete_etsy_connection(
        request: Request, response: Response, auth: CurrentAuth
    ) -> ConnectedShopView:
        """Redeems the code from the callback cookie (once: the cookie is removed whatever the
        outcome), stores the tokens sealed, creates or updates the shop (one per Etsy shop and
        organization), grants the connecting Owner, and audits the connection."""
        etsy = _etsy(request)
        session = _session(request)
        now = clock()
        sent = open_sealed(request, _callback_sealer(request), ETSY_CALLBACK_COOKIE, now=now) or {}
        expire(response, ETSY_CALLBACK_COOKIE)
        gone = {"Set-Cookie": expired_header(ETSY_CALLBACK_COOKIE)}
        try:
            connected = await complete_connection(
                etsy,
                auth,
                session_id=session,
                # No cookie: audited and refused as a missing state before any code is used.
                code=sent.get("code", ""),
                state=sent.get("state") if sent.get("code") else None,
                now=now,
                correlation_id=request_id_of(request),
            )
        except StateRejectedError:
            raise ProblemError(ETSY_STATE_REJECTED, headers=gone) from None
        except ConnectNotAllowedError as error:
            raise ProblemError(
                _PROBLEM_FOR_OUTCOME.get(error.outcome, FORBIDDEN), headers=gone
            ) from None
        except EtsyAuthorizationRefusedError:
            raise ProblemError(ETSY_AUTHORIZATION_REFUSED, headers=gone) from None
        except EtsyUnreachableError:
            raise ProblemError(ETSY_UNREACHABLE, headers=gone) from None
        except EtsyAccountHasNoShopError:
            raise ProblemError(ETSY_NO_SHOP, headers=gone) from None
        except EtsyShopConnectedElsewhereError:
            raise ProblemError(ETSY_SHOP_CONNECTED_ELSEWHERE, headers=gone) from None
        response.headers["Cache-Control"] = "no-store"
        await notices.shop_changed(
            request, NotificationKind.SHOP_CONNECTED, connected.integration.organization_id,
            connected.integration.shop_id, at=now,
        )  # fmt: skip
        probed = await _probe_after_connect(request, connected, auth, now)
        return _connected_view(connected, now, probed)

    @router.get(
        "/shop-integrations",
        summary="Marketplace connections of the organization's shops",
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "Only the Owner and Admins see connections (forbidden)",
                404: "The named organization does not exist for the caller",
                422: ("Invalid organization ID", ValidationProblem),
            }
        ),
    )
    async def list_shop_integrations(
        request: Request,
        auth: CurrentAuth,
        reader: Annotated[IntegrationReader, Depends(_reader)],
        organization_id: uuid.UUID | None = None,
    ) -> IntegrationList:
        """Status, granted scopes and token health per shop, and Etsy's remaining daily request
        budget with its floor, for the shop card; never a token."""
        now = clock()
        try:
            found = await list_integrations(auth, reader, now=now, organization_id=organization_id)
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        return IntegrationList(
            items=[_view(summary, now) for summary in found], etsy_quota=_quota_view(request)
        )

    _add_connection_routes(router, clock=clock)

    return router


def _add_connection_routes(router: APIRouter, *, clock: Callable[[], datetime]) -> None:
    """Re-check (slice 4.5) and disconnect (slice 4.7)."""

    @router.post(
        "/shop-integrations/{shop_id}/capability-checks",
        summary="Re-check what HTCO may do with a connected shop",
        responses=problem_responses(
            {
                403: "Only the Owner and Admins re-check (forbidden), or the request did not "
                "come from the web app (csrf-failed)",
                404: "No such shop connection for the caller",
                409: (
                    "The shop is not connected (shop-not-connected; `reason` says why)",
                    ShopNotConnectedProblem,
                ),
                422: ("Invalid shop ID", ValidationProblem),
                429: "Checked moments ago (capability-check-too-soon); see Retry-After",
                503: "The Etsy connection is not set up on this server (etsy-unavailable)",
            }
        ),
    )
    async def check_shop_capabilities(
        shop_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        checks: Annotated[CapabilityChecks, Depends(_checks)],
    ) -> CapabilityCheckView:
        """Read-only probes: the shop, one receipt (address fields checked for presence only),
        one listing. Shipment write permission is never probed and stays `unknown`."""
        try:
            results = await recheck_capabilities(
                checks, auth, shop_id, now=clock(), correlation_id=request_id_of(request)
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        except ShopNotConnectedError as error:
            raise not_connected(error) from None
        except RecheckTooSoonError as error:
            raise ProblemError(
                CAPABILITY_CHECK_TOO_SOON,
                headers={"Retry-After": str(error.retry_after_seconds)},
            ) from None
        return CapabilityCheckView(shop_id=shop_id, capabilities=_capability_views(results))

    @router.post(
        "/shop-integrations/{shop_id}/disconnect",
        summary="Disconnect a shop",
        responses=problem_responses(
            {
                403: "Not the Owner, no second factor, no recent step-up "
                "(forbidden, mfa-required, step-up-required), or the request did not come from "
                "the web app (csrf-failed)",
                404: "No such shop connection for the caller",
                422: ("Invalid shop ID", ValidationProblem),
                503: "The token vault is not set up on this server (etsy-unavailable)",
            }
        ),
    )
    async def disconnect_etsy_shop(
        shop_id: uuid.UUID,
        request: Request,
        # The use case authorizes `connect_shop` for the shop's own organization, so an owner of
        # several organizations needs no default one (Codex review of #49).
        auth: CurrentAuth,
        vault: Annotated[DisconnectingVault, Depends(_vault)],
    ) -> IntegrationView:
        """Deletes the tokens, marks the shop disconnected with a purge date 30 days ahead and
        audits it; no Etsy call is made for the shop afterwards. Repeating it changes nothing."""
        now = clock()
        try:
            after = await disconnect_shop(
                vault, auth, shop_id, now=now, correlation_id=request_id_of(request)
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except DisconnectNotAllowedError as error:
            raise ProblemError(_PROBLEM_FOR_OUTCOME.get(error.outcome, FORBIDDEN)) from None
        # The shop's live data (buyer names among it) leaves memory with the tokens.
        dashboard = getattr(request.app.state, "dashboard", None)
        if isinstance(dashboard, ShopDashboard):
            dashboard.cache.forget(shop_id)
        await notices.shop_changed(
            request, NotificationKind.SHOP_DISCONNECTED, after.organization_id, shop_id, at=now
        )
        return _view(IntegrationSummary.of(after), now)

    @router.delete(
        "/shops/{shop_id}",
        status_code=204,
        summary="Remove a shop and every piece of data stored about it",
        responses=problem_responses(
            {
                403: "Not the Owner, no second factor, no recent step-up "
                "(forbidden, mfa-required, step-up-required), or the request did not come from "
                "the web app (csrf-failed)",
                404: "No such shop for the caller, or it was already removed",
                422: ("Invalid shop ID", ValidationProblem),
                503: "Removing shops is not set up on this server (etsy-unavailable)",
            }
        ),
    )
    async def remove_etsy_shop(
        shop_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        remover: Annotated[ShopRemover, Depends(_remover)],
    ) -> Response:
        """Disconnects the shop (its tokens are deleted) and deletes at once its stored order
        history (orders, items, refunds, buyers, sync state), its capability results, its
        members' grants and the shop itself, in one transaction, then audits it without buyer
        data. Nothing is sent to Etsy. Cannot be undone; repeating it answers 404."""
        try:
            await remove_shop(
                remover, auth, shop_id, now=clock(), correlation_id=request_id_of(request)
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except RemoveNotAllowedError as error:
            raise ProblemError(_PROBLEM_FOR_OUTCOME.get(error.outcome, FORBIDDEN)) from None
        dashboard = getattr(request.app.state, "dashboard", None)
        if isinstance(dashboard, ShopDashboard):
            dashboard.cache.forget(shop_id)
        return Response(status_code=204)
