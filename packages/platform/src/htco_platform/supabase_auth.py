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
        "mfa_verification_failed",
        "mfa_challenge_expired",
        "mfa_factor_not_found",
    }
)


class InvalidCredentialsError(Exception):
    """The email and password, or the refresh token, were refused."""


class WeakPasswordError(Exception):
    """Supabase Auth's password policy refused the new password."""


class SamePasswordError(Exception):
    """The new password is the user's current password (`same_password`)."""


# Updating the user also fails when the user's session is gone, not only on a bad password.
_SESSION_REFUSED = _REJECTED | {"bad_jwt", "session_not_found", "user_not_found"}


class RateLimitedError(Exception):
    """Supabase Auth asked us to slow down."""


class AuthServiceError(Exception):
    """Supabase Auth could not be reached or answered unexpectedly."""


class MfaRequiredError(AuthServiceError):
    """The user has an authenticator app and the session is not `aal2` (`insufficient_aal`).

    An AuthServiceError for every caller that does not expect it."""


class WebAuthnDisabledError(AuthServiceError):
    """WebAuthn MFA is switched off in Supabase Auth (`mfa_webauthn_*_not_enabled`)."""


_WEBAUTHN_DISABLED = frozenset(
    {"mfa_webauthn_enroll_not_enabled", "mfa_webauthn_verify_not_enabled"}
)


@dataclass(frozen=True)
class TotpEnrollment:
    factor_id: str
    # The QR code to scan (an SVG data URI), and the same secret for typing it in by hand.
    qr_code: str = field(repr=False)
    secret: str = field(repr=False)
    uri: str = field(repr=False)


@dataclass(frozen=True)
class Factor:
    id: str
    factor_type: str
    verified: bool
    friendly_name: str | None


@dataclass(frozen=True)
class WebAuthnChallenge:
    """A WebAuthn MFA challenge: `create` for a new passkey, `request` to use one (issue #113)."""

    challenge_id: str
    ceremony: str
    # `credential_options` exactly as Supabase Auth sent them: for the browser, never logged.
    options: dict[str, Any] = field(repr=False)


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

    async def set_password(self, access_token: str, password: str) -> None:
        """Sets the password of the user the token belongs to (an invitation or a reset link).

        Supabase refuses it from an `aal1` session when the user has an authenticator app."""
        await self._as_user(
            "PUT", "/user", access_token, {"password": password}, refused=_SESSION_REFUSED
        )

    async def send_password_reset(self, email: str, redirect_to: str) -> None:
        """Asks Supabase to email a password reset (recovery) link that lands on `redirect_to`.

        Supabase answers the same whether or not the account exists. Errors never carry the
        email."""
        try:
            response = await self._client.post(
                f"{self._base}/recover",
                params={"redirect_to": redirect_to},
                json={"email": email},
                headers=self._headers,
            )
        except httpx.HTTPError as error:
            raise AuthServiceError("Supabase Auth is unreachable") from error
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError("Supabase Auth rate limit")
        if response.is_error:
            raise AuthServiceError(f"Supabase Auth answered {response.status_code}")

    # MFA (slice 3.5 part 2): every call acts as the user, with their access token.

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> TotpEnrollment:
        body = await self._as_user(
            "POST",
            "/factors",
            access_token,
            {"factor_type": "totp", "friendly_name": friendly_name},
        )
        try:
            totp = body["totp"]
            return TotpEnrollment(
                factor_id=str(body["id"]),
                qr_code=str(totp["qr_code"]),
                secret=str(totp["secret"]),
                uri=str(totp["uri"]),
            )
        except (KeyError, TypeError) as error:
            raise AuthServiceError("Supabase Auth answered without the enrollment") from error

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> AuthTokens:
        challenge = await self._as_user("POST", f"/factors/{factor_id}/challenge", access_token, {})
        body = await self._as_user(
            "POST",
            f"/factors/{factor_id}/verify",
            access_token,
            {"challenge_id": str(challenge.get("id", "")), "code": code},
        )
        try:
            return _tokens(body)
        except (ValueError, KeyError, TypeError) as error:
            raise AuthServiceError("Supabase Auth answered without tokens") from error

    # Passkeys (issue #113): WebAuthn MFA factors. The RP ID and origins are Auth's own config
    # (`[auth.webauthn]`); the credential passes through as the browser made it.

    async def enroll_webauthn(self, access_token: str, *, friendly_name: str) -> str:
        body = await self._as_user(
            "POST",
            "/factors",
            access_token,
            {"factor_type": "webauthn", "friendly_name": friendly_name},
        )
        factor_id = body.get("id")
        if not isinstance(factor_id, str) or not factor_id:
            raise AuthServiceError("Supabase Auth answered without the factor")
        return factor_id

    async def challenge_webauthn(self, access_token: str, factor_id: str) -> WebAuthnChallenge:
        body = await self._as_user("POST", f"/factors/{factor_id}/challenge", access_token, {})
        webauthn = body.get("webauthn")
        challenge_id = body.get("id")
        if (
            not isinstance(webauthn, dict)
            or not isinstance(challenge_id, str)
            or webauthn.get("type") not in {"create", "request"}
            or not isinstance(webauthn.get("credential_options"), dict)
        ):
            raise AuthServiceError("Supabase Auth answered without the WebAuthn challenge")
        return WebAuthnChallenge(
            challenge_id=challenge_id,
            ceremony=str(webauthn["type"]),
            options=dict(webauthn["credential_options"]),
        )

    async def verify_webauthn(
        self,
        access_token: str,
        factor_id: str,
        challenge_id: str,
        ceremony: str,
        credential: dict[str, Any],
    ) -> AuthTokens:
        body = await self._as_user(
            "POST",
            f"/factors/{factor_id}/verify",
            access_token,
            {
                "challenge_id": challenge_id,
                "webauthn": {"type": ceremony, "credential_response": credential},
            },
            refused=_REJECTED | {"validation_failed", "mfa_ip_address_mismatch"},
            # Auth v2.197.0 answers 500 for an assertion that fails validation.
            server_error_refused=True,
        )
        try:
            return _tokens(body)
        except (ValueError, KeyError, TypeError) as error:
            raise AuthServiceError("Supabase Auth answered without tokens") from error

    async def factors(self, access_token: str) -> list[Factor]:
        body = await self._as_user("GET", "/user", access_token, None)
        entries = body.get("factors") or []
        return [
            Factor(
                id=str(entry["id"]),
                factor_type=str(entry.get("factor_type", "")),
                verified=entry.get("status") == "verified",
                friendly_name=entry.get("friendly_name"),
            )
            for entry in entries
            if isinstance(entry, dict) and "id" in entry
        ]

    async def unenroll(self, access_token: str, factor_id: str) -> None:
        await self._as_user("DELETE", f"/factors/{factor_id}", access_token, None)

    async def _as_user(  # noqa: PLR0913 - the request and how to read its refusals
        self,
        method: str,
        path: str,
        access_token: str,
        body: dict[str, Any] | None,
        *,
        refused: frozenset[str] = _REJECTED,
        server_error_refused: bool = False,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                json=body,
                headers=self._headers | {"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as error:
            raise AuthServiceError("Supabase Auth is unreachable") from error
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError("Supabase Auth rate limit")
        code = _error_code(response)
        if response.is_client_error and code == "insufficient_aal":
            raise MfaRequiredError("an aal2 session is required")
        if response.is_client_error and code in _WEBAUTHN_DISABLED:
            raise WebAuthnDisabledError("WebAuthn MFA is switched off in Supabase Auth")
        if server_error_refused and response.status_code == httpx.codes.INTERNAL_SERVER_ERROR:
            raise InvalidCredentialsError("refused")
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY and code == "weak_password":
            raise WeakPasswordError("the password is too weak")
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY and code == "same_password":
            raise SamePasswordError("the password is unchanged")
        if response.is_client_error and code in refused:
            raise InvalidCredentialsError("refused")
        if response.is_error:
            raise AuthServiceError(f"Supabase Auth answered {response.status_code}")
        try:
            parsed = response.json()
        except ValueError as error:
            raise AuthServiceError("Supabase Auth answered without JSON") from error
        return parsed if isinstance(parsed, dict) else {}

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
