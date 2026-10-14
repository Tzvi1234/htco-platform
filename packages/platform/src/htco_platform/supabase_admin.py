"""Supabase Auth's admin API, for the one call the API needs: inviting a user (slice 2.6).

The secret key is used only here, for this call, and never for database queries: requests read
and write data as the signed-in user, under RLS. Supabase sends the invitation email through the
project's SMTP settings. New-style secret keys (`sb_secret_…`) go in the `apikey` header; a legacy
`service_role` JWT is also sent as a bearer token, as that key type requires.
"""

import uuid
from typing import override

import httpx

_ALREADY_REGISTERED = frozenset({"email_exists", "user_already_exists"})


class AlreadyRegisteredError(Exception):
    """The email already has an account; Supabase does not invite it again."""


class InvitationFailedError(Exception):
    """The invitation was not sent: the service is down, refused the key, or answered oddly."""


class SupabaseInviter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        supabase_url: str,
        secret_key: str,
        redirect_to: str | None,
    ) -> None:
        self._client = client
        self._url = supabase_url.rstrip("/") + "/auth/v1/invite"
        self._headers = {"apikey": secret_key}
        if secret_key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {secret_key}"
        self._params = {} if redirect_to is None else {"redirect_to": redirect_to}

    async def invite(self, email: str) -> uuid.UUID:
        try:
            response = await self._client.post(
                self._url, json={"email": email}, headers=self._headers, params=self._params
            )
        except httpx.HTTPError as error:
            raise InvitationFailedError("the sign-in service is unreachable") from error
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY and (
            _error_code(response) in _ALREADY_REGISTERED
        ):
            raise AlreadyRegisteredError("the email already has an account")
        if response.is_error:
            raise InvitationFailedError(f"the sign-in service answered {response.status_code}")
        try:
            return uuid.UUID(str(response.json()["id"]))
        except (ValueError, KeyError, TypeError) as error:
            raise InvitationFailedError("the sign-in service answered without a user ID") from error

    @override
    def __repr__(self) -> str:
        return f"SupabaseInviter(url={self._url!r}, secret_key=<hidden>)"


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error_code") or body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None
