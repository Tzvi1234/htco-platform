"""Supabase Auth's admin API, for the one call the API needs: inviting a user (slice 2.6).

The project has no email provider yet, so Supabase does not send the invitation: the admin
`generate_link` call creates (or refreshes) the invited user and returns a one-time invitation
link, which the Owner or Admin passes on themselves. The link is a bearer credential: it is never
logged and its `repr` is hidden. It expires with Supabase's email OTP expiry.

The secret key is used only here, for this call, and never for database queries: requests read
and write data as the signed-in user, under RLS. New-style secret keys (`sb_secret_…`) go in the
`apikey` header; a legacy `service_role` JWT is also sent as a bearer token, as that key type
requires.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, override

import httpx

_ALREADY_REGISTERED = frozenset({"email_exists", "user_already_exists"})


class AlreadyRegisteredError(Exception):
    """The email already has an account; Supabase does not invite it again."""


class InvitationFailedError(Exception):
    """No invitation link: the service is down, refused the key, or answered oddly."""


@dataclass(frozen=True)
class Invited:
    """The invited user and their one-time invitation link."""

    user_id: uuid.UUID
    link: str = field(repr=False)


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
        self._url = supabase_url.rstrip("/") + "/auth/v1/admin/generate_link"
        self._headers = {"apikey": secret_key}
        if secret_key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {secret_key}"
        self._redirect_to = redirect_to

    async def invite(self, email: str) -> Invited:
        body = {"type": "invite", "email": email}
        if self._redirect_to is not None:
            body["redirect_to"] = self._redirect_to
        try:
            response = await self._client.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as error:
            raise InvitationFailedError("the sign-in service is unreachable") from error
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY and (
            _error_code(response) in _ALREADY_REGISTERED
        ):
            raise AlreadyRegisteredError("the email already has an account")
        if response.is_error:
            raise InvitationFailedError(f"the sign-in service answered {response.status_code}")
        try:
            answer = response.json()
            user_id = uuid.UUID(str(answer["id"]))
        except (ValueError, KeyError, TypeError) as error:
            raise InvitationFailedError("the sign-in service answered without a user ID") from error
        link = _action_link(answer)
        if link is None:
            raise InvitationFailedError("the sign-in service answered without a link")
        return Invited(user_id=user_id, link=link)

    @override
    def __repr__(self) -> str:
        return f"SupabaseInviter(url={self._url!r}, secret_key=<hidden>)"


def _action_link(answer: dict[str, Any]) -> str | None:
    """The link at the top level (the REST API), or under `properties` (as supabase-js shows it)."""
    properties = answer.get("properties")
    candidates = [answer.get("action_link")]
    if isinstance(properties, dict):
        candidates.append(properties.get("action_link"))
    return next((c for c in candidates if isinstance(c, str) and c), None)


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error_code") or body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None
