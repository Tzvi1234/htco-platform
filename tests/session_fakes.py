"""In-memory fakes for web sessions, shared by the application and API tests (slice 3.5)."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from htco_application.access.authentication import AuthenticatedUser
from htco_application.mfa import Enrollment, Factor
from htco_application.web_sessions import (
    ClientInfo,
    CredentialsRefusedError,
    IssuedTokens,
    MfaCodeRequiredError,
    NewSession,
    ResetEmailNotSentError,
    SamePasswordError,
    SessionEndedError,
    StoredSession,
    WeakPasswordError,
)
from htco_domain.identity import AssuranceLevel

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
USER = uuid.UUID("11111111-0000-0000-0000-000000000001")
CLIENT = ClientInfo(ip="203.0.113.7", user_agent="Firefox")
# The tokens an invitation link hands the browser (in the URL fragment of the redirect).
INVITE_ACCESS = "invite-access"
INVITE_REFRESH = "invite-refresh"
NEW_PASSWORD = "a long new passphrase"  # noqa: S105 - test value
# The tokens a password reset (recovery) link hands the browser, the same way.
RESET_ACCESS = "reset-access"
RESET_REFRESH = "reset-refresh"
RESET_REDIRECT = "https://dash.ht-co.app/reset-password"
# The authenticator app's right code in `Authenticator`.
RIGHT_CODE = "123456"


def tokens(n: int, *, expires_in: timedelta = timedelta(hours=1)) -> IssuedTokens:
    return IssuedTokens(
        access_token=f"access-{n}", refresh_token=f"refresh-{n}", expires_at=NOW + expires_in
    )


@dataclass
class Auth:
    """Supabase Auth in memory: one password, numbered tokens, refresh tokens used once."""

    password: str = "correct horse battery"  # noqa: S105 - test value
    issued: int = 0
    valid_refresh: set[str] = field(default_factory=set)
    signed_out: list[str] = field(default_factory=list)
    refresh_expires_in: timedelta = timedelta(hours=1)
    passwords_set: list[tuple[str, str]] = field(default_factory=list)
    # Password reset emails asked for: (email, redirect), and whether the provider fails.
    reset_emails: list[tuple[str, str]] = field(default_factory=list)
    reset_email_fails: bool = False
    # A user with an authenticator app: Supabase wants an `aal2` session to change the password.
    mfa_enrolled: bool = False

    async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens:
        if (email, password) != ("owner@example.test", self.password):
            raise CredentialsRefusedError
        return self._issue(timedelta(hours=1))

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        if refresh_token not in self.valid_refresh:
            raise CredentialsRefusedError
        self.valid_refresh.discard(refresh_token)
        return self._issue(self.refresh_expires_in)

    async def sign_out(self, access_token: str) -> None:
        self.signed_out.append(access_token)

    async def set_password(self, access_token: str, password: str) -> None:
        """Supabase refuses an ended session, and a password its policy calls weak."""
        if access_token.startswith("revoked"):
            raise CredentialsRefusedError
        if self.mfa_enrolled and not access_token.startswith("aal2"):
            raise MfaCodeRequiredError("an aal2 session is required")
        if "weak" in password:
            raise WeakPasswordError("too weak")
        if password == self.password:
            raise SamePasswordError("the same password")
        self.passwords_set.append((access_token, password))

    async def send_password_reset(self, email: str, redirect_to: str) -> None:
        if self.reset_email_fails:
            raise ResetEmailNotSentError("the provider failed")
        self.reset_emails.append((email, redirect_to))

    def invitation(self) -> tuple[str, str]:
        """The access and refresh tokens of a session opened by an invitation link."""
        self.valid_refresh.add(INVITE_REFRESH)
        return INVITE_ACCESS, INVITE_REFRESH

    def recovery(self) -> tuple[str, str]:
        """The access and refresh tokens of a session opened by a password reset link."""
        self.valid_refresh.add(RESET_REFRESH)
        return RESET_ACCESS, RESET_REFRESH

    def _issue(self, expires_in: timedelta) -> IssuedTokens:
        self.issued += 1
        issued = tokens(self.issued, expires_in=expires_in)
        self.valid_refresh.add(issued.refresh_token)
        return issued


@dataclass
class Authenticator:
    """Supabase MFA in memory: verified authenticator apps and each one's right code."""

    auth: Auth
    enrolled: bool = True
    verified: list[tuple[str, str]] = field(default_factory=list)
    codes: dict[str, str] = field(default_factory=lambda: {"factor-1": RIGHT_CODE})

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment:
        raise NotImplementedError

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens:
        if self.codes.get(factor_id) != code:
            raise CredentialsRefusedError
        self.verified.append((access_token, factor_id))
        self.auth.valid_refresh.add("aal2-refresh")
        return IssuedTokens("aal2-access", "aal2-refresh", NOW + timedelta(hours=1))

    async def factors_of(self, access_token: str) -> list[Factor]:  # noqa: ARG002 - fake
        if not self.enrolled:
            return []
        return [Factor(id=factor, verified=True, name=factor) for factor in self.codes]

    async def remove_factor(self, access_token: str, factor_id: str) -> None:
        raise NotImplementedError


class Tokens:
    """Reads the user out of an access token (the JWT verifier in production)."""

    async def read(self, access_token: str) -> AuthenticatedUser:
        if access_token.startswith("invalid"):  # forged, expired or for another project
            raise SessionEndedError("not a valid access token")
        return AuthenticatedUser(
            user_id=USER, assurance=AssuranceLevel.AAL1, session_id=None, claims={"t": access_token}
        )


@dataclass
class Store:
    sessions: dict[uuid.UUID, StoredSession] = field(default_factory=dict)
    attempts: list[tuple[bytes, bytes | None, bool, datetime]] = field(default_factory=list)

    async def create(self, session: NewSession) -> None:
        self.sessions[session.id] = StoredSession(
            id=session.id,
            token_hash=session.token_hash,
            user_id=session.user_id,
            tokens=session.tokens,
            created_at=session.created_at,
            last_seen_at=session.created_at,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
            revoked_at=None,
            user_agent=session.user_agent,
        )

    async def find(self, token_hash: bytes) -> StoredSession | None:
        return next((s for s in self.sessions.values() if s.token_hash == token_hash), None)

    async def replace_tokens(self, session_id: uuid.UUID, issued: IssuedTokens) -> None:
        self.sessions[session_id] = replace(self.sessions[session_id], tokens=issued)

    async def touch(self, session_id: uuid.UUID, at: datetime, idle_expires_at: datetime) -> None:
        self.sessions[session_id] = replace(
            self.sessions[session_id], last_seen_at=at, idle_expires_at=idle_expires_at
        )

    async def revoke(self, session_id: uuid.UUID, at: datetime) -> None:
        self.sessions[session_id] = replace(self.sessions[session_id], revoked_at=at)

    async def set_reauthenticated(self, session_id: uuid.UUID, at: datetime) -> None:
        self.sessions[session_id] = replace(self.sessions[session_id], reauthenticated_at=at)

    async def get(self, session_id: uuid.UUID) -> StoredSession | None:
        return self.sessions.get(session_id)

    async def list_active(self, user_id: uuid.UUID, now: datetime) -> list[StoredSession]:
        return [
            s
            for s in self.sessions.values()
            if s.user_id == user_id
            and s.revoked_at is None
            and now < s.idle_expires_at
            and now < s.absolute_expires_at
        ]

    async def record_attempt(
        self, email_hash: bytes, ip_hash: bytes | None, *, succeeded: bool, at: datetime
    ) -> None:
        self.attempts.append((email_hash, ip_hash, succeeded, at))

    async def succeeded_since(self, key: bytes, since: datetime) -> bool:
        return any(a[0] == key and a[2] and a[3] >= since for a in self.attempts)

    async def recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]:
        failed = [a for a in self.attempts if not a[2] and a[3] >= since]
        by_email = sum(1 for a in failed if a[0] == email_hash)
        by_ip = sum(1 for a in failed if ip_hash is not None and a[1] == ip_hash)
        return by_email, by_ip
