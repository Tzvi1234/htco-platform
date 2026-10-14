"""Who is calling: the auth context built from a verified token and database memberships (2.3)."""

import uuid
from collections.abc import Sequence

import pytest

from htco_application.access.authentication import (
    AuthContext,
    AuthenticatedUser,
    Membership,
    NoMembershipError,
    authenticate,
)
from htco_application.access.policy import Principal
from htco_domain.identity import AssuranceLevel, Role

pytestmark = pytest.mark.anyio

USER = uuid.UUID("11111111-0000-0000-0000-000000000001")
ORG_A = uuid.UUID("a0000000-0000-0000-0000-000000000000")
ORG_B = uuid.UUID("b0000000-0000-0000-0000-000000000000")
SHOP_1 = uuid.UUID("a1000000-0000-0000-0000-000000000001")


def user(assurance: AssuranceLevel = AssuranceLevel.AAL1) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=USER,
        assurance=assurance,
        session_id="s1",
        claims={"sub": str(USER), "role": "authenticated", "aal": assurance.value},
    )


class Memberships:
    """A membership reader the test controls; it records who asked."""

    def __init__(self, *memberships: Membership) -> None:
        self.memberships = memberships
        self.asked: list[AuthenticatedUser] = []

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        self.asked.append(user)
        return self.memberships


async def test_authenticate__reads_memberships_from_the_database_for_this_user() -> None:
    reader = Memberships(Membership(ORG_A, Role.SUPPORT, frozenset({SHOP_1})))
    context = await authenticate(user(AssuranceLevel.AAL2), reader)

    assert reader.asked == [user(AssuranceLevel.AAL2)]
    assert context == AuthContext(
        user_id=USER,
        assurance=AssuranceLevel.AAL2,
        session_id="s1",
        memberships=(Membership(ORG_A, Role.SUPPORT, frozenset({SHOP_1})),),
    )


async def test_authenticate__no_membership__is_refused() -> None:
    with pytest.raises(NoMembershipError):
        await authenticate(user(), Memberships())


async def test_principal_for__a_member_organization__carries_role_shops_and_assurance() -> None:
    context = await authenticate(
        user(AssuranceLevel.AAL2),
        Memberships(
            Membership(ORG_A, Role.SUPPORT, frozenset({SHOP_1})),
            Membership(ORG_B, Role.OWNER, frozenset()),
        ),
    )

    assert context.principal_for(ORG_A) == Principal(
        organization_id=ORG_A,
        role=Role.SUPPORT,
        assurance=AssuranceLevel.AAL2,
        reauthenticated_at=None,
        granted_shops=frozenset({SHOP_1}),
    )
    assert context.principal_for(ORG_B).role is Role.OWNER


async def test_principal_for__another_organization__is_refused() -> None:
    context = await authenticate(user(), Memberships(Membership(ORG_A, Role.OWNER, frozenset())))
    with pytest.raises(NoMembershipError):
        context.principal_for(ORG_B)


async def test_default_organization__single_membership__is_that_organization() -> None:
    context = await authenticate(user(), Memberships(Membership(ORG_A, Role.VIEWER, frozenset())))
    assert context.default_organization() == ORG_A


async def test_default_organization__several_memberships__is_none() -> None:
    context = await authenticate(
        user(),
        Memberships(
            Membership(ORG_A, Role.VIEWER, frozenset()), Membership(ORG_B, Role.VIEWER, frozenset())
        ),
    )
    assert context.default_organization() is None


async def test_authenticated_user__repr__hides_the_claims() -> None:
    assert "authenticated" not in repr(user())
    assert str(USER) in repr(user())
