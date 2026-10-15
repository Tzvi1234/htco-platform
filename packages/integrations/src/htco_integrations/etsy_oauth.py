"""Etsy's OAuth 2.0 endpoints: authorization URL, code exchange and refresh (slices 4.3, 4.4).

Facts (developers.etsy.com/documentation/essentials/authentication, read 2026-10-15): the browser
goes to https://www.etsy.com/oauth/connect with `response_type=code`, `client_id` (the keystring),
`redirect_uri`, space-separated `scope`, `state` and a PKCE S256 challenge. Tokens come from a form
POST to https://api.etsy.com/v3/public/oauth/token (`authorization_code` with `code_verifier`, or
`refresh_token`); the answer has `access_token` (prefixed with the user's numeric ID and a dot),
`token_type` `Bearer`, `expires_in` (3600 s) and `refresh_token` (90 days), and sometimes the
granted `scope`. Requests also carry `x-api-key` like every v3 call.

One attempt per request, no retry: a token request is not something to repeat blindly (a code is
single-use), and the refresh provider counts failures itself. `invalid_grant` raises
`OAuthInvalidGrantError`; anything else (no answer, another error, an unreadable payload) raises
`OAuthUnavailableError`. Nothing logs or raises with a token, a code, a verifier or a secret.
"""

from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import structlog

from htco_application.etsy_connect import (
    OAuthInvalidGrantError,
    OAuthUnavailableError,
    TokenGrant,
)
from htco_integrations.etsy import EtsyCredentials

ETSY_AUTHORIZATION_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"  # noqa: S105 - a URL
_REFUSED = frozenset({httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED})

_log = structlog.get_logger(__name__)


class EtsyOAuthClient:
    """`EtsyAuthorizationServer` on Etsy."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        credentials: EtsyCredentials,
        token_url: str = ETSY_TOKEN_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._token_url = token_url
        self._timeout = httpx.Timeout(timeout_seconds)

    def authorization_url(
        self, *, state: str, code_challenge: str, scopes: frozenset[str], redirect_uri: str
    ) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._credentials.keystring,
                "redirect_uri": redirect_uri,
                "scope": " ".join(sorted(scopes)),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            quote_via=quote,
        )
        return f"{ETSY_AUTHORIZATION_URL}?{query}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenGrant:
        return await self._token(
            "authorization_code",
            {
                "grant_type": "authorization_code",
                "client_id": self._credentials.keystring,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        return await self._token(
            "refresh_token",
            {
                "grant_type": "refresh_token",
                "client_id": self._credentials.keystring,
                "refresh_token": refresh_token,
            },
        )

    async def _token(self, grant_type: str, form: dict[str, str]) -> TokenGrant:
        try:
            response = await self._client.post(
                self._token_url,
                data=form,
                headers={
                    "x-api-key": self._credentials.api_key_header(),
                    "accept": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TransportError as error:
            _log.warning("etsy.token_request_failed", grant_type=grant_type, status=None)
            raise OAuthUnavailableError(
                f"Etsy did not answer the {grant_type} request ({type(error).__name__})"
            ) from None
        body = _json(response)
        status = response.status_code
        if status in _REFUSED and body is not None and body.get("error") == "invalid_grant":
            _log.info("etsy.token_refused", grant_type=grant_type, status=status)
            raise OAuthInvalidGrantError(f"Etsy refused the {grant_type} grant (invalid_grant)")
        if not response.is_success or body is None:
            _log.warning("etsy.token_request_failed", grant_type=grant_type, status=status)
            raise OAuthUnavailableError(f"Etsy answered {status} to the {grant_type} request")
        try:
            return _grant(body)
        except KeyError, TypeError, ValueError:
            _log.warning("etsy.token_request_failed", grant_type=grant_type, status=status)
            raise OAuthUnavailableError(
                f"Etsy answered the {grant_type} request with an unexpected payload"
            ) from None


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _grant(body: dict[str, Any]) -> TokenGrant:
    access, refresh, expires_in = body["access_token"], body["refresh_token"], body["expires_in"]
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise TypeError("tokens must be strings")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        raise TypeError("expires_in must be an integer")
    if str(body.get("token_type", "")).lower() != "bearer":
        raise ValueError("only bearer tokens are supported")
    scope = body.get("scope")
    scopes = None
    if isinstance(scope, str) and scope.strip():
        scopes = frozenset(scope.split())
    return TokenGrant(
        access_token=access,
        refresh_token=refresh,
        expires_in=timedelta(seconds=expires_in),
        scopes=scopes,
    )
