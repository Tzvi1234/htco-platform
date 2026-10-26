"""Two-factor sign-in, step-up and the session list for the web app (slice 3.5 part 2).

Enrolling an authenticator app and verifying its first code upgrade the session to `aal2`: the
new tokens replace the old ones on the server, and the moment counts as a re-authentication.
Step-up re-enters a code later; the policy engine accepts the session as recently re-authenticated
for ten minutes (AC-3.5.7). Wrong codes are throttled per user like wrong passwords: each code
is reserved against the user's limit before Supabase verifies it.

Users see and revoke their own sessions; revoking one also ends it at Supabase, so that device is
signed out on its next request (AC-3.5.4).
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from htco_application import web_sessions
from htco_application.web_sessions import (
    ActiveSession,
    AuthProvider,
    ClientInfo,
    CredentialsRefusedError,
    IssuedTokens,
    SessionStore,
    StoredSession,
    Throttled,
)
from htco_domain.identity import AssuranceLevel

MAX_CODE_FAILURES = 5

# Shared with the password reset, where a code can be entered too.
MfaCodeRefusedError = web_sessions.MfaCodeRefusedError


class MfaNotEnrolledError(Exception):
    """The user has no verified authenticator to step up with."""


class SessionNotFoundError(Exception):
    """No such active session of this user."""


@dataclass(frozen=True)
class Factor:
    id: str
    verified: bool
    name: str | None


@dataclass(frozen=True)
class Enrollment:
    factor_id: str
    qr_code: str = field(repr=False)
    secret: str = field(repr=False)
    uri: str = field(repr=False)


@dataclass(frozen=True)
class MfaStatus:
    assurance: AssuranceLevel
    factors: list[Factor]

    @property
    def enrolled(self) -> bool:
        return any(factor.verified for factor in self.factors)


@dataclass(frozen=True)
class SessionSummary:
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    current: bool


class MfaProvider(Protocol):
    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment: ...

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens: ...

    async def factors_of(self, access_token: str) -> list[Factor]: ...

    async def remove_factor(self, access_token: str, factor_id: str) -> None: ...


class SessionDirectory(SessionStore, Protocol):
    async def get(self, session_id: uuid.UUID) -> StoredSession | None: ...

    async def list_active(self, user_id: uuid.UUID, now: datetime) -> list[StoredSession]: ...


def mfa_failure_key(user_id: uuid.UUID) -> bytes:
    """Wrong codes are counted per user under this key, wherever the code was entered."""
    return hashlib.sha256(f"mfa:{user_id}".encode()).digest()


async def mfa_status(session: ActiveSession, mfa: MfaProvider) -> MfaStatus:
    return MfaStatus(
        assurance=session.user.assurance,
        factors=await mfa.factors_of(session.tokens.access_token),
    )


async def start_enrollment(
    session: ActiveSession, mfa: MfaProvider, *, friendly_name: str
) -> Enrollment:
    """Unfinished factors (a QR code shown but never confirmed) are removed first: Supabase refuses
    a second factor with the same name, which would block every later attempt. A name already
    used by a verified factor gets a number."""
    access_token = session.tokens.access_token
    kept: set[str | None] = set()
    for factor in await mfa.factors_of(access_token):
        if factor.verified:
            kept.add(factor.name)
        else:
            await mfa.remove_factor(access_token, factor.id)
    name, number = friendly_name, 2
    while name in kept:
        name, number = f"{friendly_name} ({number})", number + 1
    return await mfa.enroll_totp(access_token, friendly_name=name)


async def verify_enrollment(  # noqa: PLR0913 - the code, its factor and the collaborators
    session: ActiveSession,
    mfa: MfaProvider,
    store: SessionStore,
    factor_id: str,
    code: str,
    *,
    client: ClientInfo,
    now: datetime,
) -> None:
    await _verify(session, mfa, store, factor_id, code, client=client, now=now)


async def step_up(  # noqa: PLR0913 - the code and the collaborators
    session: ActiveSession,
    mfa: MfaProvider,
    store: SessionStore,
    code: str,
    *,
    client: ClientInfo,
    now: datetime,
) -> None:
    factors = await mfa.factors_of(session.tokens.access_token)
    verified = next((factor for factor in factors if factor.verified), None)
    if verified is None:
        raise MfaNotEnrolledError("enroll an authenticator app first")
    await _verify(session, mfa, store, verified.id, code, client=client, now=now)


async def _verify(  # noqa: PLR0913 - the code, its factor and the collaborators
    session: ActiveSession,
    mfa: MfaProvider,
    store: SessionStore,
    factor_id: str,
    code: str,
    *,
    client: ClientInfo,
    now: datetime,
) -> None:
    key = mfa_failure_key(session.user.user_id)
    ip_hash = hashlib.sha256(client.ip.encode()).digest() if client.ip else None
    # The address is recorded (failed codes count against it at sign-in) but not limited here.
    async with Throttled(
        store,
        key,
        ip_hash,
        max_by_key=MAX_CODE_FAILURES,
        max_by_ip=None,
        now=now,
        failures=(MfaCodeRefusedError,),
    ):
        try:
            issued = await mfa.verify_totp(session.tokens.access_token, factor_id, code)
        except CredentialsRefusedError:
            raise MfaCodeRefusedError("wrong code") from None
        await store.replace_tokens(session.session_id, issued)
        await store.set_reauthenticated(session.session_id, now)


async def list_sessions(
    session: ActiveSession, store: SessionDirectory, *, now: datetime
) -> list[SessionSummary]:
    return [
        SessionSummary(
            id=stored.id,
            created_at=stored.created_at,
            last_seen_at=stored.last_seen_at,
            user_agent=stored.user_agent,
            current=stored.id == session.session_id,
        )
        for stored in await store.list_active(session.user.user_id, now)
    ]


async def revoke_session(
    session: ActiveSession,
    store: SessionDirectory,
    auth: AuthProvider,
    session_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    target = await store.get(session_id)
    if target is None or target.user_id != session.user.user_id or target.revoked_at is not None:
        raise SessionNotFoundError("no such session")
    await store.revoke(target.id, now)
    await auth.sign_out(target.tokens.access_token)
