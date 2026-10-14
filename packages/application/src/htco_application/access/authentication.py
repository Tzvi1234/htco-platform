"""Who is calling (slice 2.3): a verified user plus the memberships the database holds for them.

Authorization never trusts what a token says about roles or organizations (claims go stale, and
user metadata is editable by the user). The token proves only who the user is and how strongly
the session signed in; organizations, roles and granted shops are read from the database on every
request, so a revoked membership stops working on the next request (AC-2.6.3).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.policy import Principal
from htco_domain.identity import AssuranceLevel, Role


class NoMembershipError(Exception):
    """The user belongs to no organization, or not to the one the request names."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """A user whose access token was verified. `claims` feed the database's RLS context."""

    user_id: uuid.UUID
    assurance: AssuranceLevel
    session_id: str | None
    claims: Mapping[str, Any] = field(repr=False)
    # When the session last re-entered its second factor (web sessions; step-up, AC-3.5.7).
    reauthenticated_at: datetime | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Membership:
    organization_id: uuid.UUID
    role: Role
    granted_shops: frozenset[uuid.UUID]


class MembershipReader(Protocol):
    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]: ...


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    assurance: AssuranceLevel
    session_id: str | None
    memberships: tuple[Membership, ...]
    # The verified token claims, for repositories that query as this user (RLS).
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    reauthenticated_at: datetime | None = None

    def principal_for(self, organization_id: uuid.UUID) -> Principal:
        """The user acting in one organization; refused when they are not a member of it."""
        for membership in self.memberships:
            if membership.organization_id == organization_id:
                return Principal(
                    organization_id=organization_id,
                    role=membership.role,
                    assurance=self.assurance,
                    reauthenticated_at=self.reauthenticated_at,
                    granted_shops=membership.granted_shops,
                )
        raise NoMembershipError("not a member of this organization")

    def default_organization(self) -> uuid.UUID | None:
        """The organization when the user has exactly one; otherwise the request must name it."""
        if len(self.memberships) == 1:
            return self.memberships[0].organization_id
        return None


async def authenticate(user: AuthenticatedUser, memberships: MembershipReader) -> AuthContext:
    """Build the auth context; a user without any membership has no business in the API."""
    found = tuple(await memberships.memberships_of(user))
    if not found:
        raise NoMembershipError("the user belongs to no organization")
    return AuthContext(
        user_id=user.user_id,
        assurance=user.assurance,
        session_id=user.session_id,
        memberships=found,
        claims=user.claims,
        reauthenticated_at=user.reauthenticated_at,
    )
