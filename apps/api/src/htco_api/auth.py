"""Authentication for API routes (slice 2.3): `CurrentAuth` gives a route the verified caller.

The bearer token is verified against the auth provider's keys; the caller's organizations, roles
and shops are then read from the database inside the caller's own RLS context. Each failure
answers with its own problem type (AC-2.3.1); a verified user with no membership gets 403
(AC-2.3.2). Rejections are logged with their reason only, never the token.
"""

from typing import Annotated, Protocol

import structlog
from fastapi import Depends, Request

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
from htco_application.access.authentication import (
    AuthContext,
    AuthenticatedUser,
    MembershipReader,
    NoMembershipError,
    authenticate,
)
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
        user = AuthenticatedUser(
            user_id=verified.subject,
            assurance=AssuranceLevel(verified.assurance),
            session_id=verified.session_id,
            claims=verified.claims,
        )
        try:
            return await authenticate(user, self._memberships)
        except NoMembershipError:
            raise _rejected(NO_MEMBERSHIP) from None


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _rejected(kind: ProblemType, headers: dict[str, str] | None = None) -> ProblemError:
    _log.info("auth.rejected", problem=kind.slug)
    return ProblemError(kind, headers=headers)


async def require_auth(request: Request) -> AuthContext:
    authenticator = getattr(request.app.state, "authenticator", None)
    if not isinstance(authenticator, Authenticator):
        _log.warning("auth.unavailable", reason="no authenticator configured")
        raise ProblemError(AUTH_UNAVAILABLE)
    return await authenticator.authenticate(request.headers.get("authorization"))


CurrentAuth = Annotated[AuthContext, Depends(require_auth)]
