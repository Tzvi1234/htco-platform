"""Access-token verification against the auth provider's published keys (slice 2.3, ASVS V9).

Supabase signs user access tokens with an asymmetric key (ES256) and publishes the public keys as
a JWKS. A token is accepted only when it is signed by one of those keys with ES256 (never `none`
or an HMAC algorithm), names our issuer and audience, has not expired, and belongs to a signed-in
user (`role` = `authenticated`). Each rejection names one `TokenProblem`, so the API can answer
with a precise problem type without leaking anything else.

The key set is cached for a bounded time. An unknown key ID triggers one refresh (the provider
may have rotated its key), at most once per cooldown so forged key IDs cannot flood the provider.
Stale keys are never used: when the key set cannot be refreshed, verification fails closed.
"""

import contextlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, override

import anyio
import httpx
import jwt

_ALGORITHM = "ES256"
_USER_ROLE = "authenticated"
_ASSURANCE_LEVELS = frozenset({"aal1", "aal2"})


class TokenProblem(StrEnum):
    MALFORMED = "malformed"
    EXPIRED = "expired"
    WRONG_AUDIENCE = "wrong_audience"
    WRONG_ISSUER = "wrong_issuer"
    BAD_SIGNATURE = "bad_signature"
    UNKNOWN_KEY = "unknown_key"
    MISSING_CLAIM = "missing_claim"
    NOT_A_USER = "not_a_user"


class InvalidTokenError(Exception):
    """The token was rejected; `problem` says why. The token itself is never kept."""

    def __init__(self, problem: TokenProblem) -> None:
        super().__init__(f"invalid access token: {problem.value}")
        self.problem = problem


class KeySetUnavailableError(Exception):
    """The provider's key set could not be loaded, so no token can be verified right now."""


@dataclass(frozen=True)
class VerifiedToken:
    """What a valid token proves: who the user is and how strongly the session signed in."""

    subject: uuid.UUID
    assurance: str
    session_id: str | None
    expires_at: datetime
    # The verified claims, for the database's `request.jwt.claims` in user-scoped transactions.
    claims: Mapping[str, Any]


class JwksSource(Protocol):
    async def fetch(self) -> Mapping[str, Any]: ...


class HttpJwksSource:
    """Fetches the JWKS document over HTTP (a client from `htco_platform.http`)."""

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self._client = client
        self._url = url

    async def fetch(self) -> Mapping[str, Any]:
        response = await self._client.get(self._url)
        response.raise_for_status()
        document: Mapping[str, Any] = response.json()
        return document


@dataclass(frozen=True)
class KeyCachePolicy:
    """How long fetched keys are trusted, and how often an unknown key ID may trigger a fetch."""

    max_age_seconds: float = 600.0
    refresh_cooldown_seconds: float = 30.0


class JwtVerifier:
    DEFAULT_MAX_AGE_SECONDS = KeyCachePolicy.max_age_seconds
    DEFAULT_REFRESH_COOLDOWN_SECONDS = KeyCachePolicy.refresh_cooldown_seconds

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: JwksSource,
        cache: KeyCachePolicy = KeyCachePolicy(),  # noqa: B008 - frozen, shared safely
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._max_age = cache.max_age_seconds
        self._cooldown = cache.refresh_cooldown_seconds
        self._monotonic = monotonic
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = anyio.Lock()

    async def verify(self, token: str) -> VerifiedToken:
        kid = self._key_id(token)
        key = await self._key(kid)
        claims = self._decode(token, key)
        return self._verified(claims)

    @staticmethod
    def _key_id(token: str) -> str:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.DecodeError as error:
            raise InvalidTokenError(TokenProblem.MALFORMED) from error
        if header.get("alg") != _ALGORITHM:
            raise InvalidTokenError(TokenProblem.BAD_SIGNATURE)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError(TokenProblem.UNKNOWN_KEY)
        return kid

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            if self._expired():
                self._keys, self._fetched_at = {}, None
                await self._refresh()
            elif kid not in self._keys and self._may_retry():
                # Maybe a rotated key. If the refresh fails, the current keys stay valid.
                with contextlib.suppress(KeySetUnavailableError):
                    await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError(TokenProblem.UNKNOWN_KEY)
        return key

    def _expired(self) -> bool:
        return self._fetched_at is None or self._monotonic() - self._fetched_at > self._max_age

    def _may_retry(self) -> bool:
        return self._attempted_at is None or self._monotonic() - self._attempted_at > self._cooldown

    async def _refresh(self) -> None:
        self._attempted_at = self._monotonic()
        try:
            document = await self._jwks.fetch()
        except (httpx.HTTPError, ValueError) as error:
            raise KeySetUnavailableError(
                "the auth provider's key set could not be fetched"
            ) from error
        keys = _signing_keys(document)
        if keys is None:
            raise KeySetUnavailableError("the auth provider's key set is malformed")
        self._keys, self._fetched_at = keys, self._attempted_at

    def _decode(self, token: str, key: jwt.PyJWK) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "aud", "iss", "role"]},
            )
        except jwt.ExpiredSignatureError as error:
            raise InvalidTokenError(TokenProblem.EXPIRED) from error
        except jwt.InvalidAudienceError as error:
            raise InvalidTokenError(TokenProblem.WRONG_AUDIENCE) from error
        except jwt.InvalidIssuerError as error:
            raise InvalidTokenError(TokenProblem.WRONG_ISSUER) from error
        except jwt.MissingRequiredClaimError as error:
            raise InvalidTokenError(TokenProblem.MISSING_CLAIM) from error
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError) as error:
            raise InvalidTokenError(TokenProblem.BAD_SIGNATURE) from error
        except jwt.InvalidTokenError as error:
            raise InvalidTokenError(TokenProblem.MALFORMED) from error
        return claims

    @staticmethod
    def _verified(claims: dict[str, Any]) -> VerifiedToken:
        if claims["role"] != _USER_ROLE:
            raise InvalidTokenError(TokenProblem.NOT_A_USER)
        try:
            subject = uuid.UUID(str(claims["sub"]))
        except ValueError as error:
            raise InvalidTokenError(TokenProblem.MALFORMED) from error
        assurance = claims.get("aal", "aal1")
        if assurance not in _ASSURANCE_LEVELS:
            raise InvalidTokenError(TokenProblem.MALFORMED)
        session_id = claims.get("session_id")
        return VerifiedToken(
            subject=subject,
            assurance=str(assurance),
            session_id=session_id if isinstance(session_id, str) else None,
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
            claims=claims,
        )

    @override
    def __repr__(self) -> str:
        return f"JwtVerifier(issuer={self._issuer!r}, audience={self._audience!r})"


def _signing_keys(document: object) -> dict[str, jwt.PyJWK] | None:
    """ES256 public keys by key ID; other keys are skipped. None when the document is malformed."""
    if not isinstance(document, Mapping):
        return None
    entries = document.get("keys")
    if not isinstance(entries, list):
        return None
    keys: dict[str, jwt.PyJWK] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        kid = entry.get("kid")
        if entry.get("kty") != "EC" or entry.get("alg", _ALGORITHM) != _ALGORITHM:
            continue
        if not isinstance(kid, str):
            continue
        try:
            keys[kid] = jwt.PyJWK(dict(entry), algorithm=_ALGORITHM)
        except jwt.PyJWKError, jwt.InvalidKeyError, KeyError, TypeError, ValueError:
            continue
    return keys
