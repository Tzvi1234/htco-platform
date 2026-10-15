"""Connecting an Etsy shop (slice 4.3) and the shop card's connection health (slice 4.4).

`POST /v1/shops/etsy/connect`: the organization's Owner, signed in with a second factor and
stepped up in the last ten minutes, from the web app (cookie session, CSRF header and origin). The
answer is Etsy's authorization URL; the browser goes there.

`POST /v1/integrations/etsy/callback`: the web app's `/integrations/etsy/callback` page posts the
`code` and `state` Etsy put in its URL, as a same-site request with the session cookie. The state
must belong to this browser session; a rejected callback is audited.

`GET /v1/shop-integrations`: Owner and Admin see each connection's status, scopes and token health.

No response carries a token, a code or a verifier (AC-4.2.3); connect answers are `no-store`.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from htco_api.auth import CurrentAuth, web_session_id
from htco_api.identity import ORGANIZATION_REQUIRED, ShopView, problem_responses
from htco_api.policy_guard import FORBIDDEN, MFA_REQUIRED, require_capability
from htco_api.problems import (
    NOT_FOUND,
    NOT_READY,
    ProblemError,
    ProblemType,
    ValidationProblem,
    request_id_of,
)
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
from htco_application.shop_integrations import (
    IntegrationReader,
    IntegrationSummary,
    list_integrations,
)
from htco_application.shops import OrganizationRequiredError, ShopAccessDeniedError
from htco_domain.integrations import IntegrationProvider, IntegrationStatus, TokenHealth

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
_PROBLEM_FOR_OUTCOME = {Outcome.MFA_REQUIRED: MFA_REQUIRED}


class AuthorizationView(BaseModel):
    # Send the browser here; it returns to the web app's callback route.
    authorization_url: str


class EtsyCallback(BaseModel):
    # From the callback URL's query, exactly as Etsy sent them.
    code: str = Field(min_length=1, max_length=2048)
    # Missing or blank is accepted here and rejected (and audited) as a missing state.
    state: str | None = Field(default=None, max_length=512)


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


class IntegrationList(BaseModel):
    items: list[IntegrationView]


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
    )


def _connected_view(connected: ConnectedShop, now: datetime) -> ConnectedShopView:
    return ConnectedShopView(
        shop=ShopView(**vars(connected.shop)),
        integration=_view(IntegrationSummary.of(connected.integration), now),
        reconnected=connected.reconnected,
    )


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


def _reader(request: Request) -> IntegrationReader:
    reader = getattr(request.app.state, "integrations", None)
    if reader is None:
        raise ProblemError(NOT_READY)
    found: IntegrationReader = reader
    return found


def etsy_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["etsy"])
    Owner = Annotated[AuthContext, Depends(require_capability(Capability.CONNECT_SHOP))]  # noqa: N806

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
        request: Request, response: Response, auth: Owner
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

    @router.post(
        "/integrations/etsy/callback",
        summary="Finish connecting an Etsy shop",
        responses=problem_responses(
            {
                400: "The state is missing, unknown, used, expired or from another session "
                "(etsy-state-rejected), Etsy refused the code (etsy-authorization-refused), "
                "or the body is not valid JSON",
                403: "Not the Owner with a second factor (forbidden, mfa-required), not a web "
                "session (web-session-required), or not from the web app (csrf-failed)",
                409: "The Etsy account has no shop (etsy-no-shop), or the shop is connected to "
                "another organization (etsy-shop-connected-elsewhere)",
                422: ("Missing or oversized code", ValidationProblem),
                502: "Etsy did not answer usefully; nothing was saved (etsy-unreachable)",
                503: "Sign-in, the database or the Etsy connection is unavailable "
                "(etsy-unavailable)",
            }
        ),
    )
    async def complete_etsy_connection(
        body: EtsyCallback, request: Request, response: Response, auth: CurrentAuth
    ) -> ConnectedShopView:
        """Exchanges the code, stores the tokens sealed, creates or updates the shop (one per
        Etsy shop and organization), grants the connecting Owner, and audits the connection."""
        etsy = _etsy(request)
        session = _session(request)
        now = clock()
        try:
            connected = await complete_connection(
                etsy,
                auth,
                session_id=session,
                code=body.code,
                state=body.state,
                now=now,
                correlation_id=request_id_of(request),
            )
        except StateRejectedError:
            raise ProblemError(ETSY_STATE_REJECTED) from None
        except ConnectNotAllowedError as error:
            raise ProblemError(_PROBLEM_FOR_OUTCOME.get(error.outcome, FORBIDDEN)) from None
        except EtsyAuthorizationRefusedError:
            raise ProblemError(ETSY_AUTHORIZATION_REFUSED) from None
        except EtsyUnreachableError:
            raise ProblemError(ETSY_UNREACHABLE) from None
        except EtsyAccountHasNoShopError:
            raise ProblemError(ETSY_NO_SHOP) from None
        except EtsyShopConnectedElsewhereError:
            raise ProblemError(ETSY_SHOP_CONNECTED_ELSEWHERE) from None
        response.headers["Cache-Control"] = "no-store"
        return _connected_view(connected, now)

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
        auth: CurrentAuth,
        reader: Annotated[IntegrationReader, Depends(_reader)],
        organization_id: uuid.UUID | None = None,
    ) -> IntegrationList:
        """Status, granted scopes and token health per shop, for the shop card; never a token."""
        now = clock()
        try:
            found = await list_integrations(auth, reader, now=now, organization_id=organization_id)
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        return IntegrationList(items=[_view(summary, now) for summary in found])

    return router
