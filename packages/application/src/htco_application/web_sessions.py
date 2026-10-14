"""Web sessions for the backend-for-frontend (slice 3.5, ADR-0006).

The browser holds one opaque, random cookie value; the server keeps only its SHA-256 hash, and
next to it the user's Supabase tokens (sealed at rest by the store). Each request resumes the
session: it must be known, not revoked, and inside both its idle and absolute lifetimes; an access
token close to expiry is refreshed here, so the browser never sees a token (AC-3.5.1). A refresh
that Supabase refuses (the session was revoked elsewhere) ends the session (AC-3.5.4).

Sign-in failures are counted per email and per client address over a short window; past the
limit, sign-in is refused before Supabase is asked, and the refusal is the same whether or not
the email exists (AC-3.5.5).
"""

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.access.authentication import AuthenticatedUser

IDLE_LIFETIME = timedelta(hours=12)
ABSOLUTE_LIFETIME = timedelta(days=14)
REFRESH_MARGIN = timedelta(minutes=2)
THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20
# Writing `last_seen_at` on every request is wasteful; this much activity is enough to record.
TOUCH_INTERVAL = timedelta(minutes=1)


class CredentialsRefusedError(Exception):
    """The auth provider refused the password or the refresh token."""


class SignInRefusedError(Exception):
    """Wrong email or password. Deliberately says nothing about which."""


class TooManyAttemptsError(Exception):
    """Too many failed sign-ins for this email or from this address; try again later."""


class SessionEndedError(Exception):
    """No usable session: unknown cookie, revoked, expired, or refused by the auth provider."""


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class NewSession:
    id: uuid.UUID
    token_hash: bytes = field(repr=False)
    user_id: uuid.UUID
    tokens: IssuedTokens
    created_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    user_agent: str | None


@dataclass(frozen=True)
class StoredSession:
    id: uuid.UUID
    token_hash: bytes = field(repr=False)
    user_id: uuid.UUID
    tokens: IssuedTokens
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    user_agent: str | None


@dataclass(frozen=True)
class SignedIn:
    cookie_value: str = field(repr=False)
    session_id: uuid.UUID
    user: AuthenticatedUser
    expires_at: datetime


@dataclass(frozen=True)
class ActiveSession:
    session_id: uuid.UUID
    user: AuthenticatedUser


class AuthProvider(Protocol):
    async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens: ...

    async def refresh(self, refresh_token: str) -> IssuedTokens: ...

    async def sign_out(self, access_token: str) -> None: ...


class TokenReader(Protocol):
    """Verifies an access token and says who it belongs to."""

    async def read(self, access_token: str) -> AuthenticatedUser: ...


class SessionStore(Protocol):
    async def create(self, session: NewSession) -> None: ...

    async def find(self, token_hash: bytes) -> StoredSession | None: ...

    async def replace_tokens(self, session_id: uuid.UUID, issued: IssuedTokens) -> None: ...

    async def touch(
        self, session_id: uuid.UUID, at: datetime, idle_expires_at: datetime
    ) -> None: ...

    async def revoke(self, session_id: uuid.UUID, at: datetime) -> None: ...

    async def record_attempt(
        self, email_hash: bytes, ip_hash: bytes | None, *, succeeded: bool, at: datetime
    ) -> None: ...

    async def recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]: ...


def hash_cookie(cookie_value: str) -> bytes:
    return hashlib.sha256(cookie_value.encode("utf-8")).digest()


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


async def sign_in(  # noqa: PLR0913 - the credentials, the client and the collaborators
    email: str,
    password: str,
    *,
    client: ClientInfo,
    auth: AuthProvider,
    tokens: TokenReader,
    store: SessionStore,
    now: datetime,
) -> SignedIn:
    address = email.strip().lower()
    email_hash = _hash(address)
    ip_hash = _hash(client.ip) if client.ip else None
    by_email, by_ip = await store.recent_failures(email_hash, ip_hash, now - THROTTLE_WINDOW)
    if by_email >= MAX_FAILURES_PER_EMAIL or by_ip >= MAX_FAILURES_PER_IP:
        raise TooManyAttemptsError("too many failed sign-ins")
    try:
        issued = await auth.sign_in_with_password(address, password)
    except CredentialsRefusedError:
        await store.record_attempt(email_hash, ip_hash, succeeded=False, at=now)
        raise SignInRefusedError("wrong email or password") from None
    user = await tokens.read(issued.access_token)
    cookie_value = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    session = NewSession(
        id=uuid.uuid7(),
        token_hash=hash_cookie(cookie_value),
        user_id=user.user_id,
        tokens=issued,
        created_at=now,
        idle_expires_at=now + IDLE_LIFETIME,
        absolute_expires_at=now + ABSOLUTE_LIFETIME,
        user_agent=(client.user_agent or "")[:300] or None,
    )
    await store.create(session)
    await store.record_attempt(email_hash, ip_hash, succeeded=True, at=now)
    return SignedIn(
        cookie_value=cookie_value,
        session_id=session.id,
        user=user,
        expires_at=session.idle_expires_at,
    )


async def resume_session(
    cookie_value: str,
    *,
    auth: AuthProvider,
    tokens: TokenReader,
    store: SessionStore,
    now: datetime,
) -> ActiveSession:
    session = await store.find(hash_cookie(cookie_value)) if cookie_value else None
    if session is None or session.revoked_at is not None:
        raise SessionEndedError("no active session")
    if now >= session.idle_expires_at or now >= session.absolute_expires_at:
        await store.revoke(session.id, now)
        raise SessionEndedError("the session expired")
    issued = session.tokens
    if issued.expires_at - now <= REFRESH_MARGIN:
        try:
            issued = await auth.refresh(issued.refresh_token)
        except CredentialsRefusedError:
            await store.revoke(session.id, now)
            raise SessionEndedError("the auth provider ended the session") from None
        await store.replace_tokens(session.id, issued)
    if now - session.last_seen_at >= TOUCH_INTERVAL:
        idle_expires_at = min(now + IDLE_LIFETIME, session.absolute_expires_at)
        await store.touch(session.id, now, idle_expires_at)
    return ActiveSession(session_id=session.id, user=await tokens.read(issued.access_token))


async def sign_out(
    cookie_value: str, *, auth: AuthProvider, store: SessionStore, now: datetime
) -> None:
    session = await store.find(hash_cookie(cookie_value)) if cookie_value else None
    if session is None or session.revoked_at is not None:
        return
    await store.revoke(session.id, now)
    await auth.sign_out(session.tokens.access_token)
