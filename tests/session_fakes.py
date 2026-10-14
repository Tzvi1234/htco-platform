"""In-memory fakes for web sessions, shared by the application and API tests (slice 3.5)."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from htco_application.access.authentication import AuthenticatedUser
from htco_application.web_sessions import (
    ClientInfo,
    CredentialsRefusedError,
    IssuedTokens,
    NewSession,
    StoredSession,
)
from htco_domain.identity import AssuranceLevel

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
USER = uuid.UUID("11111111-0000-0000-0000-000000000001")
CLIENT = ClientInfo(ip="203.0.113.7", user_agent="Firefox")


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

    def _issue(self, expires_in: timedelta) -> IssuedTokens:
        self.issued += 1
        issued = tokens(self.issued, expires_in=expires_in)
        self.valid_refresh.add(issued.refresh_token)
        return issued


class Tokens:
    """Reads the user out of an access token (the JWT verifier in production)."""

    async def read(self, access_token: str) -> AuthenticatedUser:
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

    async def record_attempt(
        self, email_hash: bytes, ip_hash: bytes | None, *, succeeded: bool, at: datetime
    ) -> None:
        self.attempts.append((email_hash, ip_hash, succeeded, at))

    async def recent_failures(
        self, email_hash: bytes, ip_hash: bytes | None, since: datetime
    ) -> tuple[int, int]:
        failed = [a for a in self.attempts if not a[2] and a[3] >= since]
        by_email = sum(1 for a in failed if a[0] == email_hash)
        by_ip = sum(1 for a in failed if ip_hash is not None and a[1] == ip_hash)
        return by_email, by_ip
