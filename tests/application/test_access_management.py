"""User and shop-access management (slice 2.6, AC-2.6.1, AC-2.6.2 and AC-2.6.4)."""

import uuid
from typing import override

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.management import (
    AccessDeniedError,
    GrantNotFoundError,
    InviteeAlreadyRegisteredError,
    MemberAlreadyExistsError,
    MemberRow,
    MembershipNotFoundError,
    MfaRequiredError,
    ShopNotInOrganizationError,
    change_role,
    grant_shop,
    invite_member,
    list_members,
    remove_member,
    revoke_shop,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from tests.access_fakes import (
    ADMIN,
    FOREIGN_SHOP,
    INVITE_LINK,
    M_ADMIN,
    M_ELSEWHERE,
    M_OWNER,
    M_SUPPORT,
    NEWCOMER,
    NOW,
    ORG,
    OTHER_ORG,
    OWNER,
    SHOP,
    SUPPORT,
    Inviter,
    Store,
)

pytestmark = pytest.mark.anyio


def acting(user: uuid.UUID, role: Role, aal: AssuranceLevel = AssuranceLevel.AAL2) -> AuthContext:
    return AuthContext(
        user_id=user,
        assurance=aal,
        session_id=None,
        memberships=(Membership(ORG, role, frozenset()),),
        claims={"sub": str(user)},
    )


AS_OWNER = acting(OWNER, Role.OWNER)
AS_ADMIN = acting(ADMIN, Role.ADMIN)


# AC-2.6.1: only Owner and Admin, with an aal2 session --------------------------------------------


@pytest.mark.parametrize(
    "role", [Role.OPERATIONS, Role.FINANCE, Role.SUPPORT, Role.DEVELOPER, Role.VIEWER]
)
async def test_every_other_role__is_denied_every_change(role: Role) -> None:
    store, actor = Store(), acting(SUPPORT, role)
    attempts = [
        invite_member(
            actor,
            store,
            Inviter(),
            ORG,
            email="n@x.test",
            role=Role.VIEWER,
            now=NOW,
            correlation_id="c",
        ),
        change_role(actor, store, ORG, M_SUPPORT, Role.VIEWER, now=NOW, correlation_id="c"),
        remove_member(actor, store, ORG, M_SUPPORT, now=NOW, correlation_id="c"),
        grant_shop(actor, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c"),
        revoke_shop(actor, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c"),
        list_members(actor, store, ORG, now=NOW),
    ]
    for attempt in attempts:
        with pytest.raises(AccessDeniedError):
            await attempt
    assert store.audit == []


@pytest.mark.parametrize(
    "actor",
    [
        acting(OWNER, Role.OWNER, AssuranceLevel.AAL1),
        acting(ADMIN, Role.ADMIN, AssuranceLevel.AAL1),
    ],
)
async def test_owner_and_admin_with_aal1__need_mfa(actor: AuthContext) -> None:
    with pytest.raises(MfaRequiredError):
        await change_role(actor, Store(), ORG, M_SUPPORT, Role.VIEWER, now=NOW, correlation_id="c")


async def test_another_organization__is_denied() -> None:
    with pytest.raises(AccessDeniedError):
        await list_members(AS_OWNER, Store(), OTHER_ORG, now=NOW)


# Invitations -------------------------------------------------------------------------------------


async def test_invite__creates_the_membership_and_audits_it() -> None:
    store, inviter = Store(), Inviter()
    invited = await invite_member(
        AS_ADMIN,
        store,
        inviter,
        ORG,
        email=" New@Example.test ",
        role=Role.SUPPORT,
        now=NOW,
        correlation_id="corr",
    )
    member = invited.member

    assert inviter.invited == ["new@example.test"]
    assert (member.user_id, member.role, member.organization_id) == (NEWCOMER, Role.SUPPORT, ORG)
    [record] = store.audit
    assert record.actor == ActorRef(ActorKind.USER, str(ADMIN))
    assert (record.action, record.entity_type, record.entity_id) == (
        "membership.created",
        "membership",
        str(member.id),
    )
    assert record.before is None
    assert record.after == {"user_id": str(NEWCOMER), "role": "support"}
    assert record.correlation_id == "corr"
    # The membership check and the write both run as the caller (RLS applies to each).
    assert store.claims_seen == [{"sub": str(ADMIN)}, {"sub": str(ADMIN)}]


async def test_invite__someone_already_in_the_organization__conflict() -> None:
    store = Store()
    with pytest.raises(MemberAlreadyExistsError):
        await invite_member(
            AS_OWNER,
            store,
            Inviter(SUPPORT),
            ORG,
            email="s@x.test",
            role=Role.VIEWER,
            now=NOW,
            correlation_id="c",
        )
    assert store.audit == []


async def test_invite__a_member_again__refused_without_replacing_the_delivered_link() -> None:
    """A retried invitation (its first response lost) must not ask the sign-in service for a new
    link: that would replace the pending token and break the link already sent."""
    store = Store(registered={"s@x.test": SUPPORT})
    inviter = Inviter(SUPPORT)
    with pytest.raises(MemberAlreadyExistsError):
        await invite_member(
            AS_OWNER,
            store,
            inviter,
            ORG,
            email="s@x.test",
            role=Role.VIEWER,
            now=NOW,
            correlation_id="c",
        )
    assert inviter.invited == []
    assert store.audit == []


async def test_invite__already_registered_user__is_reported() -> None:
    with pytest.raises(InviteeAlreadyRegisteredError):
        await invite_member(
            AS_OWNER,
            Store(),
            Inviter(registered=True),
            ORG,
            email="s@x.test",
            role=Role.VIEWER,
            now=NOW,
            correlation_id="c",
        )


@pytest.mark.parametrize("email", ["", "no-at-sign", "@x.test", "a@"])
async def test_invite__invalid_email__refused_before_inviting(email: str) -> None:
    inviter = Inviter()
    with pytest.raises(ValueError, match="email"):
        await invite_member(
            AS_OWNER,
            Store(),
            inviter,
            ORG,
            email=email,
            role=Role.VIEWER,
            now=NOW,
            correlation_id="c",
        )
    assert inviter.invited == []


async def test_invite__returns_the_one_time_link_for_the_inviter_to_pass_on() -> None:
    """No email provider yet: the sign-in service creates the link and the caller shares it."""
    invited = await invite_member(
        AS_OWNER,
        Store(),
        Inviter(),
        ORG,
        email="new@example.test",
        role=Role.ADMIN,
        now=NOW,
        correlation_id="c",
    )
    assert invited.invite_link == INVITE_LINK
    assert invited.member.role is Role.ADMIN
    assert INVITE_LINK not in repr(invited)


async def test_invite__email_already_registered__added_without_a_link() -> None:
    store = Store(registered={"known@example.test": NEWCOMER})
    invited = await invite_member(
        AS_OWNER,
        store,
        Inviter(registered=True),
        ORG,
        email="known@example.test",
        role=Role.ADMIN,
        now=NOW,
        correlation_id="c",
    )
    assert invited.invite_link is None
    assert invited.member.user_id == NEWCOMER


# AC-2.6.2: an Admin cannot grant the Owner role or remove the Owner -----------------------------


async def test_admin__cannot_invite_an_owner() -> None:
    inviter = Inviter()
    with pytest.raises(AccessDeniedError):
        await invite_member(
            AS_ADMIN,
            Store(),
            inviter,
            ORG,
            email="n@x.test",
            role=Role.OWNER,
            now=NOW,
            correlation_id="c",
        )
    assert inviter.invited == []


async def test_admin__cannot_make_anyone_owner() -> None:
    with pytest.raises(AccessDeniedError):
        await change_role(
            AS_ADMIN, Store(), ORG, M_SUPPORT, Role.OWNER, now=NOW, correlation_id="c"
        )


@pytest.mark.parametrize("actor", [AS_ADMIN, AS_OWNER])
async def test_nobody__can_remove_or_demote_the_owner(actor: AuthContext) -> None:
    store = Store()
    with pytest.raises(AccessDeniedError):
        await remove_member(actor, store, ORG, M_OWNER, now=NOW, correlation_id="c")
    with pytest.raises(AccessDeniedError):
        await change_role(actor, store, ORG, M_OWNER, Role.ADMIN, now=NOW, correlation_id="c")
    assert store.members[M_OWNER].role is Role.OWNER


async def test_owner__can_promote_and_demote_admins() -> None:
    store = Store()
    await change_role(AS_OWNER, store, ORG, M_SUPPORT, Role.ADMIN, now=NOW, correlation_id="c")
    await change_role(AS_OWNER, store, ORG, M_ADMIN, Role.VIEWER, now=NOW, correlation_id="c")
    assert store.members[M_SUPPORT].role is Role.ADMIN
    assert store.members[M_ADMIN].role is Role.VIEWER


# AC-2.6.4: one audit record per change, with actor, target, before and after -------------------


async def test_change_role__audits_before_and_after() -> None:
    store = Store()
    member = await change_role(
        AS_OWNER, store, ORG, M_SUPPORT, Role.FINANCE, now=NOW, correlation_id="c"
    )

    assert member.role is Role.FINANCE
    [record] = store.audit
    assert (record.action, record.entity_id) == ("membership.role_changed", str(M_SUPPORT))
    assert record.before == {"role": "support"}
    assert record.after == {"role": "finance"}
    assert record.actor == ActorRef(ActorKind.USER, str(OWNER))


async def test_change_role__to_the_same_role__changes_and_audits_nothing() -> None:
    store = Store()
    await change_role(AS_OWNER, store, ORG, M_SUPPORT, Role.SUPPORT, now=NOW, correlation_id="c")
    assert store.audit == []


async def test_remove_member__revokes_and_audits() -> None:
    store = Store()
    await remove_member(AS_ADMIN, store, ORG, M_SUPPORT, now=NOW, correlation_id="c")

    assert M_SUPPORT not in store.members
    [record] = store.audit
    assert (record.action, record.entity_id) == ("membership.revoked", str(M_SUPPORT))
    assert record.before == {"user_id": str(SUPPORT), "role": "support"}
    assert record.after == {"revoked_at": NOW.isoformat()}


async def test_grant_and_revoke_shop__audited_once_each_and_idempotent() -> None:
    store = Store()
    await grant_shop(AS_ADMIN, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")
    await grant_shop(AS_ADMIN, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")
    assert list(store.grants) == [(M_SUPPORT, SHOP)]

    await revoke_shop(AS_ADMIN, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")
    assert store.grants == {}

    granted, revoked = store.audit
    assert (granted.action, granted.entity_type) == ("shop_access.granted", "shop_access")
    assert granted.after == {"membership_id": str(M_SUPPORT), "shop_id": str(SHOP)}
    assert (revoked.action, revoked.before) == (
        "shop_access.revoked",
        {"membership_id": str(M_SUPPORT), "shop_id": str(SHOP)},
    )
    assert revoked.entity_id == granted.entity_id


async def test_revoke_shop__not_granted__not_found() -> None:
    with pytest.raises(GrantNotFoundError):
        await revoke_shop(AS_ADMIN, Store(), ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")


@pytest.mark.parametrize("shop", [FOREIGN_SHOP, uuid.UUID(int=5)], ids=["other-tenant", "unknown"])
async def test_grant_shop__shop_outside_the_organization__not_found(shop: uuid.UUID) -> None:
    store = Store()
    with pytest.raises(ShopNotInOrganizationError):
        await grant_shop(AS_OWNER, store, ORG, M_SUPPORT, shop, now=NOW, correlation_id="c")
    assert store.grants == {}


@pytest.mark.parametrize(
    "membership", [M_ELSEWHERE, uuid.UUID(int=7)], ids=["other-tenant", "unknown"]
)
async def test_membership_outside_the_organization__not_found(membership: uuid.UUID) -> None:
    store = Store()
    for attempt in (
        change_role(AS_OWNER, store, ORG, membership, Role.VIEWER, now=NOW, correlation_id="c"),
        remove_member(AS_OWNER, store, ORG, membership, now=NOW, correlation_id="c"),
        grant_shop(AS_OWNER, store, ORG, membership, SHOP, now=NOW, correlation_id="c"),
    ):
        with pytest.raises(MembershipNotFoundError):
            await attempt


async def test_list_members__the_organizations_members_with_their_shops() -> None:
    store = Store()
    await grant_shop(AS_OWNER, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")
    members = await list_members(AS_ADMIN, store, ORG, now=NOW)

    assert [(m.id, m.role) for m in members] == [
        (M_OWNER, Role.OWNER),
        (M_ADMIN, Role.ADMIN),
        (M_SUPPORT, Role.SUPPORT),
    ]
    assert members[2].shop_ids == frozenset({SHOP})


# Review fixes ------------------------------------------------------------------------------------


async def test_invite__email_already_registered__adds_that_user() -> None:
    """An invitation interrupted after the sign-in service created the user can be retried, and a
    member of another organization can be added."""
    store = Store(registered={"known@example.test": NEWCOMER})
    invited = await invite_member(
        AS_OWNER,
        store,
        Inviter(registered=True),
        ORG,
        email="Known@Example.test",
        role=Role.VIEWER,
        now=NOW,
        correlation_id="c",
    )
    member = invited.member
    assert member.user_id == NEWCOMER
    [record] = store.audit
    assert record.after == {"user_id": str(NEWCOMER), "role": "viewer"}


async def test_admin__can_remove_their_own_membership() -> None:
    """The audit record is written while the actor is still a member."""
    store = Store()
    await remove_member(AS_ADMIN, store, ORG, M_ADMIN, now=NOW, correlation_id="c")
    assert M_ADMIN not in store.members
    assert [record.action for record in store.audit] == ["membership.revoked"]


async def test_grant__lost_race_with_an_identical_grant__is_a_quiet_success() -> None:
    class Racing(Store):
        @override
        def grant_of(
            self,
            membership_id: uuid.UUID,
            shop_id: uuid.UUID,
        ) -> uuid.UUID | None:
            return None  # read before the other request's insert committed

    store = Racing()
    store.grants[(M_SUPPORT, SHOP)] = uuid.UUID(int=99)
    await grant_shop(AS_ADMIN, store, ORG, M_SUPPORT, SHOP, now=NOW, correlation_id="c")
    assert store.audit == []


async def test_concurrent_promotion_to_owner__the_admins_change_is_refused() -> None:
    """The Admin read the member as Support; by the write it is the Owner: the database refuses."""

    class Promoted(Store):
        @override
        def member(self, organization_id: uuid.UUID, membership_id: uuid.UUID) -> MemberRow | None:
            stale = super().member(organization_id, membership_id)
            self.update_role(M_SUPPORT, Role.OWNER)  # the Owner's promotion commits meanwhile
            return stale

    store = Promoted()
    with pytest.raises(AccessDeniedError):
        await remove_member(AS_ADMIN, store, ORG, M_SUPPORT, now=NOW, correlation_id="c")
    assert store.audit == []
