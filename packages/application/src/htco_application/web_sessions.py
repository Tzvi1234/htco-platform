"""Web sessions for the backend-for-frontend (slice 3.5, ADR-0006).

The browser holds one opaque, random cookie value; the server keeps only its SHA-256 hash, and
next to it the user's Supabase tokens (sealed at rest by the store). Each request resumes the
session: it must be known, not revoked, and inside both its idle and absolute lifetimes; an access
token close to expiry is refreshed here, so the browser never sees a token (AC-3.5.1). A refresh
that Supabase refuses (the session was revoked elsewhere) ends the session (AC-3.5.4).

Sign-in failures are counted per email and per client address over a short window; past the
limit, sign-in is refused before Supabase is asked, and the refusal is the same whether or not
the email exists (AC-3.5.5).

Accepting an invitation opens the same kind of session. The invitation link lands on the web app
with the invited user's Supabase tokens; the web app posts them once with the chosen password.
The access token is verified, the password set as that user, the refresh token exchanged for fresh
tokens, and the session opened as at sign-in. Failures are counted per link and per client address
in the same attempt log as sign-ins, so failed sign-ins and refused links share the address limit.
A password reset link works the same way (`set_password_from_link`; see `password_reset`).
"""

import base64
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.access.authentication import AuthenticatedUser

IDLE_LIFETIME = timedelta(hours=12)
ABSOLUTE_LIFETIME = timedelta(days=14)
REFRESH_MARGIN = timedelta(minutes=2)
THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20
# A link that already opened a session is refused for this long, even if its access token is
# still valid; longer than any access token or email link lives.
LINK_REUSE_WINDOW = timedelta(days=1)
# Writing `last_seen_at` on every request is wasteful; this much activity is enough to record.
TOUCH_INTERVAL = timedelta(minutes=1)


class CredentialsRefusedError(Exception):
    """The auth provider refused the password or the refresh token."""


class SignInRefusedError(Exception):
    """Wrong email or password. Deliberately says nothing about which."""


class InvitationRefusedError(Exception):
    """The invitation link's tokens are not valid (forged, expired, used, or the user is gone)."""


class ResetLinkRefusedError(Exception):
    """The password reset link's tokens are not valid (forged, expired, used, or user gone)."""


class MfaCodeRequiredError(Exception):
    """The user has an authenticator app: the auth provider wants a code before the password
    changes."""


class MfaCodeRefusedError(Exception):
    """The authenticator app's code is wrong or expired."""


class ResetEmailNotSentError(Exception):
    """The auth provider did not send the password reset email (refused, rate limited or down)."""


class SamePasswordError(Exception):
    """The new password is the account's current password."""


class WeakPasswordError(Exception):
    """The auth provider's password policy refused the new password."""


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
    reauthenticated_at: datetime | None = None


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
    # The session's current tokens, for calls made as the user (MFA).
    tokens: IssuedTokens = field(repr=False)


class AuthProvider(Protocol):
    async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens: ...

    async def refresh(self, refresh_token: str) -> IssuedTokens: ...

    async def sign_out(self, access_token: str) -> None: ...

    async def set_password(self, access_token: str, password: str) -> None:
        """As the token's user. Raises WeakPasswordError, CredentialsRefusedError when the
        user's session is not valid any more, or MfaCodeRequiredError when the user has an
        authenticator app and the session is not `aal2`."""
        ...

    async def send_password_reset(self, email: str, redirect_to: str) -> None:
        """Emails a password reset link that lands on `redirect_to`. Says nothing about whether
        the account exists; raises ResetEmailNotSentError when the email was not sent."""
        ...


class TokenReader(Protocol):
    """Verifies an access token and says who it belongs to; SessionEndedError when not valid."""

    async def read(self, access_token: str) -> AuthenticatedUser: ...


class SessionStore(Protocol):
    async def create(self, session: NewSession) -> None: ...

    async def find(self, token_hash: bytes) -> StoredSession | None: ...

    async def replace_tokens(self, session_id: uuid.UUID, issued: IssuedTokens) -> None: ...

    async def touch(
        self, session_id: uuid.UUID, at: datetime, idle_expires_at: datetime
    ) -> None: ...

    async def revoke(self, session_id: uuid.UUID, at: datetime) -> None: ...

    async def set_reauthenticated(self, session_id: uuid.UUID, at: datetime) -> None: ...

    async def record_attempt(
        self, email_hash: bytes, ip_hash: bytes | None, *, succeeded: bool, at: datetime
    ) -> None: ...

    async def recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]: ...

    async def succeeded_since(self, key: bytes, since: datetime) -> bool:
        """Whether an attempt under `key` succeeded at or after `since`."""
        ...


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
    signed_in = await _open_session(issued, user, client=client, store=store, now=now)
    await store.record_attempt(email_hash, ip_hash, succeeded=True, at=now)
    return signed_in


async def accept_invitation(  # noqa: PLR0913 - the link's tokens, the password and the collaborators
    access_token: str,
    refresh_token: str,
    password: str,
    *,
    client: ClientInfo,
    auth: AuthProvider,
    tokens: TokenReader,
    store: SessionStore,
    now: datetime,
) -> SignedIn:
    """Sets the invited user's password and signs them in on this browser."""
    return await set_password_from_link(
        access_token,
        refresh_token,
        password,
        link=INVITATION_LINK,
        client=client,
        auth=auth,
        tokens=tokens,
        store=store,
        now=now,
    )


@dataclass(frozen=True)
class PasswordLink:
    """A kind of link that lets its user choose a password (an invitation, a password reset)."""

    # Namespaces the link's throttle key in the attempt log.
    namespace: str
    refused: type[Exception]


INVITATION_LINK = PasswordLink("invitation", InvitationRefusedError)
RESET_LINK = PasswordLink("password-reset", ResetLinkRefusedError)

# Turns the link's session into an `aal2` one with the user's authenticator app, when needed.
Elevate = Callable[[str, AuthenticatedUser], Awaitable[IssuedTokens | None]]


async def set_password_from_link(  # noqa: PLR0913 - the link's tokens, the password and the collaborators
    access_token: str,
    refresh_token: str,
    password: str,
    *,
    link: PasswordLink,
    client: ClientInfo,
    auth: AuthProvider,
    tokens: TokenReader,
    store: SessionStore,
    now: datetime,
    elevate: Elevate | None = None,
) -> SignedIn:
    """Sets the password as the link's user and opens a session on this browser.

    The access token is verified, the password set as that user, the refresh token exchanged for
    fresh tokens (spending the link's session) and the session opened as at sign-in. Refused
    links and wrong codes count per link and per address; a weak or unchanged password or a missing
    code does not, so the page can ask again with the same link. A link that already opened a
    session is refused before anything changes.
    """
    link_hash = _hash(f"{link.namespace}:{access_token}")
    ip_hash = _hash(client.ip) if client.ip else None
    by_link, by_ip = await store.recent_failures(link_hash, ip_hash, now - THROTTLE_WINDOW)
    if by_link >= MAX_FAILURES_PER_EMAIL or by_ip >= MAX_FAILURES_PER_IP:
        raise TooManyAttemptsError("too many failed attempts")

    async def failed(error: Exception) -> Exception:
        await store.record_attempt(link_hash, ip_hash, succeeded=False, at=now)
        return error

    # A used link's access token can outlive it; refuse before the password changes again.
    if await store.succeeded_since(link_hash, now - LINK_REUSE_WINDOW):
        raise await failed(link.refused("the link was already used"))

    try:
        linked = await tokens.read(access_token)
        elevated = None if elevate is None else await elevate(access_token, linked)
        if elevated is not None:
            access_token, refresh_token = elevated.access_token, elevated.refresh_token
        await auth.set_password(access_token, password)
        issued = await auth.refresh(refresh_token)
        user = await tokens.read(issued.access_token)
    except SessionEndedError, CredentialsRefusedError:
        raise await failed(link.refused("the link is not valid")) from None
    except MfaCodeRefusedError:
        raise await failed(MfaCodeRefusedError("wrong code")) from None
    if user.user_id != linked.user_id:
        raise await failed(link.refused("the link is not valid"))
    signed_in = await _open_session(issued, user, client=client, store=store, now=now)
    await store.record_attempt(link_hash, ip_hash, succeeded=True, at=now)
    return signed_in


async def _open_session(
    issued: IssuedTokens,
    user: AuthenticatedUser,
    *,
    client: ClientInfo,
    store: SessionStore,
    now: datetime,
) -> SignedIn:
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
    user = await tokens.read(issued.access_token)
    return ActiveSession(
        session_id=session.id,
        user=replace(user, reauthenticated_at=session.reauthenticated_at),
        tokens=issued,
    )


async def sign_out(
    cookie_value: str, *, auth: AuthProvider, store: SessionStore, now: datetime
) -> None:
    session = await store.find(hash_cookie(cookie_value)) if cookie_value else None
    if session is None or session.revoked_at is not None:
        return
    await store.revoke(session.id, now)
    await auth.sign_out(session.tokens.access_token)
