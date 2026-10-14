"""Supabase Auth's REST API as the BFF uses it (slice 3.5, assumption A-11).

The web app never talks to Supabase Auth: the API signs the user in with the password grant,
keeps the tokens on the server and refreshes them there. Only the publishable key is sent; it
identifies the project and grants nothing by itself.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

_REJECTED = frozenset(
    {
        "invalid_credentials",
        "invalid_grant",
        "refresh_token_not_found",
        "refresh_token_already_used",
    }
)


class InvalidCredentialsError(Exception):
    """The email and password, or the refresh token, were refused."""


class RateLimitedError(Exception):
    """Supabase Auth asked us to slow down."""


class AuthServiceError(Exception):
    """Supabase Auth could not be reached or answered unexpectedly."""


@dataclass(frozen=True)
class AuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime


class SupabaseAuth:
    def __init__(
        self, client: httpx.AsyncClient, *, supabase_url: str, publishable_key: str
    ) -> None:
        self._client = client
        self._base = supabase_url.rstrip("/") + "/auth/v1"
        self._headers = {"apikey": publishable_key}

    async def sign_in_with_password(self, email: str, password: str) -> AuthTokens:
        return await self._token("password", {"email": email, "password": password})

    async def refresh(self, refresh_token: str) -> AuthTokens:
        return await self._token("refresh_token", {"refresh_token": refresh_token})

    async def sign_out(self, access_token: str) -> None:
        """Ends this session at Supabase. An already expired or revoked session is fine."""
        try:
            await self._client.post(
                f"{self._base}/logout",
                params={"scope": "local"},
                headers=self._headers | {"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as error:
            raise AuthServiceError("Supabase Auth is unreachable") from error

    async def _token(self, grant: str, body: dict[str, str]) -> AuthTokens:
        try:
            response = await self._client.post(
                f"{self._base}/token",
                params={"grant_type": grant},
                json=body,
                headers=self._headers,
            )
        except httpx.HTTPError as error:
            raise AuthServiceError("Supabase Auth is unreachable") from error
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError("Supabase Auth rate limit")
        refused = response.status_code in {httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED}
        if refused and _error_code(response) in _REJECTED:
            raise InvalidCredentialsError("credentials refused")
        if response.is_error:
            raise AuthServiceError(f"Supabase Auth answered {response.status_code}")
        try:
            return _tokens(response.json())
        except (ValueError, KeyError, TypeError) as error:
            raise AuthServiceError("Supabase Auth answered without tokens") from error


def _tokens(body: dict[str, Any]) -> AuthTokens:
    if "expires_at" in body:
        expires_at = datetime.fromtimestamp(int(body["expires_at"]), UTC)
    else:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))
    access, refresh = body["access_token"], body["refresh_token"]
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise TypeError("tokens must be strings")
    return AuthTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error_code") or body.get("error")
    return code if isinstance(code, str) else None
