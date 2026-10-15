"""Authentication for API routes (slice 2.3): `CurrentAuth` gives a route the verified caller.

The bearer token is verified against the auth provider's keys; the caller's organizations, roles
and shops are then read from the database inside the caller's own RLS context. Each failure
answers with its own problem type (AC-2.3.1); a verified user with no membership gets 403
(AC-2.3.2). Rejections are logged with their reason only, never the token.
"""

import uuid
from typing import Annotated, Protocol

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from htco_api.problems import (
    AUTH_UNAVAILABLE,
    NO_MEMBERSHIP,
    TOKEN_BAD_SIGNATURE,
    TOKEN_EXPIRED,
    TOKEN_INVALID,
    TOKEN_WRONG_AUDIENCE,
    TOKEN_WRONG_ISSUER,
    UNAUTHENTICATED,
    ProblemError,
    ProblemType,
)
from htco_api.web_auth import SESSION_COOKIE, WebAuth, resume
from htco_application.access.authentication import (
    AuthContext,
    AuthenticatedUser,
    MembershipReader,
    NoMembershipError,
    authenticate,
)
from htco_application.web_sessions import SessionEndedError
from htco_domain.identity import AssuranceLevel
from htco_platform.jwt import InvalidTokenError, KeySetUnavailableError, TokenProblem, VerifiedToken

_log = structlog.get_logger(__name__)

_PROBLEM_FOR = {
    TokenProblem.EXPIRED: TOKEN_EXPIRED,
    TokenProblem.WRONG_AUDIENCE: TOKEN_WRONG_AUDIENCE,
    TokenProblem.WRONG_ISSUER: TOKEN_WRONG_ISSUER,
    TokenProblem.BAD_SIGNATURE: TOKEN_BAD_SIGNATURE,
    TokenProblem.UNKNOWN_KEY: TOKEN_BAD_SIGNATURE,
}
# RFC 6750 §3: a missing token gets a bare challenge; a rejected one says `invalid_token`.
_CHALLENGE = {"WWW-Authenticate": "Bearer"}
_INVALID_TOKEN_CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedToken: ...


class Authenticator:
    def __init__(self, verifier: TokenVerifier, memberships: MembershipReader) -> None:
        self._verifier = verifier
        self._memberships = memberships

    async def authenticate(self, authorization: str | None) -> AuthContext:
        token = _bearer_token(authorization)
        if token is None:
            raise _rejected(UNAUTHENTICATED, _CHALLENGE)
        try:
            verified = await self._verifier.verify(token)
        except InvalidTokenError as error:
            kind = _PROBLEM_FOR.get(error.problem, TOKEN_INVALID)
            raise _rejected(kind, _INVALID_TOKEN_CHALLENGE) from None
        except KeySetUnavailableError:
            _log.warning("auth.unavailable", reason="key set unavailable")
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return await self.authenticate_user(_user_of(verified))

    async def authenticate_user(self, user: AuthenticatedUser) -> AuthContext:
        """The auth context of a user already verified (a bearer token, or a web session)."""
        try:
            return await authenticate(user, self._memberships)
        except NoMembershipError:
            raise _rejected(NO_MEMBERSHIP) from None


def _user_of(verified: VerifiedToken) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=verified.subject,
        assurance=AssuranceLevel(verified.assurance),
        session_id=verified.session_id,
        claims=verified.claims,
    )


class VerifiedTokenReader:
    """Reads the user out of a web session's access token, verifying it like a bearer token."""

    def __init__(self, verifier: TokenVerifier) -> None:
        self._verifier = verifier

    async def read(self, access_token: str) -> AuthenticatedUser:
        try:
            return _user_of(await self._verifier.verify(access_token))
        except InvalidTokenError:
            raise SessionEndedError("the session's access token is not valid") from None
        except KeySetUnavailableError:
            raise ProblemError(AUTH_UNAVAILABLE) from None


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _rejected(kind: ProblemType, headers: dict[str, str] | None = None) -> ProblemError:
    _log.info("auth.rejected", problem=kind.slug)
    return ProblemError(kind, headers=headers)


# Documents the scheme in the OpenAPI contract; `Authenticator` parses the header itself so every
# failure gets its own problem type.
_BEARER = HTTPBearer(auto_error=False, bearerFormat="JWT", description="Supabase access token")


async def require_auth(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
) -> AuthContext:
    authorization = request.headers.get("authorization")
    web = getattr(request.app.state, "web_auth", None)
    has_bearer = _bearer_token(authorization) is not None
    has_cookie = isinstance(web, WebAuth) and bool(request.cookies.get(SESSION_COOKIE))
    if not has_bearer and not has_cookie:  # no credentials: 401 even when sign-in is unavailable
        raise _rejected(UNAUTHENTICATED, _CHALLENGE)
    authenticator = getattr(request.app.state, "authenticator", None)
    if not isinstance(authenticator, Authenticator):
        _log.warning("auth.unavailable", reason="no authenticator configured")
        raise ProblemError(AUTH_UNAVAILABLE)
    if has_bearer:
        return await authenticator.authenticate(authorization)
    if not isinstance(web, WebAuth):  # pragma: no cover - has_cookie implies it
        raise ProblemError(AUTH_UNAVAILABLE)
    active = await resume(request, web, request.app.state.clock())
    # Flows bound to the browser session (the Etsy connect flow) read it from here.
    request.state.web_session_id = active.session_id
    return await authenticator.authenticate_user(active.user)


def web_session_id(request: Request) -> uuid.UUID | None:
    """The web session behind this request's cookie; None for bearer requests."""
    found = getattr(request.state, "web_session_id", None)
    return found if isinstance(found, uuid.UUID) else None


CurrentAuth = Annotated[AuthContext, Depends(require_auth)]
