"""User and shop-access management (slice 2.6, FR-IAM-01, FR-IAM-03).

Every operation needs the `manage_access` capability for the organization, which the matrix gives
only to Owner and Admin, with an `aal2` session (AC-2.6.1). Two rules sit on top of the matrix
(AC-2.6.2): only the Owner may grant the Owner role, and nobody changes or removes the Owner's
membership through the API (an ownership transfer is a separate, later flow). Each change writes
one audit record by the acting user in the same transaction (AC-2.6.4). Memberships and shops of
another organization do not exist for the caller (404).

Because authorization reads memberships from the database on every request (slice 2.3), a revoked
membership or shop grant stops working on the user's next request, even while their access token
is still valid (AC-2.6.3).
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, Principal, decide
from htco_application.audit import AuditRecord, AuditWriter
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role


class AccessDeniedError(Exception):
    """The caller may not make this change."""


class MfaRequiredError(Exception):
    """The caller may make this change only from a session with a second factor (aal2)."""


class MembershipNotFoundError(Exception):
    """No such membership in this organization."""


class ShopNotInOrganizationError(Exception):
    """No such shop in this organization."""


class GrantNotFoundError(Exception):
    """The membership has no grant for this shop."""


class MemberAlreadyExistsError(Exception):
    """The invited user is already a member of this organization."""


class InviteeAlreadyRegisteredError(Exception):
    """The email already has an account that the sign-in service will not invite again."""


@dataclass(frozen=True)
class MemberRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    display_name: str | None
    shop_ids: frozenset[uuid.UUID]


class AccessUnitOfWork(AuditWriter, Protocol):
    def member(self, organization_id: uuid.UUID, membership_id: uuid.UUID) -> MemberRow | None: ...

    def members_of(self, organization_id: uuid.UUID) -> Sequence[MemberRow]: ...

    def active_member_by_user(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool: ...

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None: ...

    def update_role(self, membership_id: uuid.UUID, role: Role) -> None: ...

    def revoke_membership(self, membership_id: uuid.UUID, at: datetime) -> None: ...

    def shop_in_organization(self, organization_id: uuid.UUID, shop_id: uuid.UUID) -> bool: ...

    def grant_of(self, membership_id: uuid.UUID, shop_id: uuid.UUID) -> uuid.UUID | None: ...

    def create_grant(
        self,
        grant_id: uuid.UUID,
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        shop_id: uuid.UUID,
    ) -> None: ...

    def delete_grant(self, grant_id: uuid.UUID) -> None: ...


class AccessStore(Protocol):
    """Runs one unit of work in a single transaction as the calling user (RLS applies)."""

    async def run[T](
        self, claims: Mapping[str, Any], work: Callable[[AccessUnitOfWork], T]
    ) -> T: ...


class Inviter(Protocol):
    """Sends the sign-in service's invitation email and returns the new user's ID."""

    async def invite(self, email: str) -> uuid.UUID: ...


def _authorize(auth: AuthContext, organization_id: uuid.UUID, now: datetime) -> Principal:
    try:
        principal = auth.principal_for(organization_id)
    except NoMembershipError:
        raise AccessDeniedError("not a member of this organization") from None
    decision = decide(principal, Capability.MANAGE_ACCESS, shop=None, now=now)
    if decision.outcome is Outcome.MFA_REQUIRED:
        raise MfaRequiredError("managing access needs an aal2 session")
    if decision.outcome is not Outcome.ALLOW:
        raise AccessDeniedError("this role cannot manage access")
    return principal


def _may_assign(principal: Principal, role: Role) -> None:
    if role is Role.OWNER and principal.role is not Role.OWNER:
        raise AccessDeniedError("only the owner can grant the owner role")


def _target(
    uow: AccessUnitOfWork, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> MemberRow:
    member = uow.member(organization_id, membership_id)
    if member is None:
        raise MembershipNotFoundError("no such membership")
    return member


def _changeable(member: MemberRow) -> None:
    if member.role is Role.OWNER:
        raise AccessDeniedError("the owner's membership cannot be changed or removed")


def _audit(  # noqa: PLR0913 - the columns of one audit record
    uow: AccessUnitOfWork,
    auth: AuthContext,
    organization_id: uuid.UUID,
    action: str,
    entity: tuple[str, uuid.UUID],
    *,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    correlation_id: str,
) -> None:
    uow.append_audit(
        AuditRecord.new(
            organization_id=organization_id,
            actor=ActorRef(ActorKind.USER, str(auth.user_id)),
            action=action,
            entity_type=entity[0],
            entity_id=str(entity[1]),
            before=before,
            after=after,
            correlation_id=correlation_id,
        )
    )


def _valid_email(email: str) -> str:
    normalized = email.strip().lower()
    local, at, domain = normalized.partition("@")
    if not (local and at and domain) or " " in normalized:
        raise ValueError("email must be an email address")
    return normalized


async def list_members(
    auth: AuthContext, store: AccessStore, organization_id: uuid.UUID, *, now: datetime
) -> Sequence[MemberRow]:
    _authorize(auth, organization_id, now)
    return await store.run(auth.claims, lambda uow: list(uow.members_of(organization_id)))


async def invite_member(  # noqa: PLR0913 - the request's fields plus the collaborators
    auth: AuthContext,
    store: AccessStore,
    inviter: Inviter,
    organization_id: uuid.UUID,
    *,
    email: str,
    role: Role,
    now: datetime,
    correlation_id: str,
    new_id: Callable[[], uuid.UUID] = uuid.uuid7,
) -> MemberRow:
    address = _valid_email(email)
    principal = _authorize(auth, organization_id, now)
    _may_assign(principal, role)
    user_id = await inviter.invite(address)
    membership_id = new_id()

    def work(uow: AccessUnitOfWork) -> MemberRow:
        if uow.active_member_by_user(organization_id, user_id):
            raise MemberAlreadyExistsError("already a member of this organization")
        uow.create_membership(membership_id, organization_id, user_id, role)
        _audit(
            uow,
            auth,
            organization_id,
            "membership.created",
            ("membership", membership_id),
            after={"user_id": str(user_id), "role": role.value},
            correlation_id=correlation_id,
        )
        return MemberRow(membership_id, organization_id, user_id, role, None, frozenset())

    return await store.run(auth.claims, work)


async def change_role(  # noqa: PLR0913 - the request's fields plus the collaborators
    auth: AuthContext,
    store: AccessStore,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    role: Role,
    *,
    now: datetime,
    correlation_id: str,
) -> MemberRow:
    principal = _authorize(auth, organization_id, now)
    _may_assign(principal, role)

    def work(uow: AccessUnitOfWork) -> MemberRow:
        member = _target(uow, organization_id, membership_id)
        _changeable(member)
        if member.role is role:
            return member
        uow.update_role(membership_id, role)
        _audit(
            uow,
            auth,
            organization_id,
            "membership.role_changed",
            ("membership", membership_id),
            before={"role": member.role.value},
            after={"role": role.value},
            correlation_id=correlation_id,
        )
        return MemberRow(
            member.id,
            member.organization_id,
            member.user_id,
            role,
            member.display_name,
            member.shop_ids,
        )

    return await store.run(auth.claims, work)


async def remove_member(  # noqa: PLR0913 - the request's fields plus the collaborators
    auth: AuthContext,
    store: AccessStore,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    *,
    now: datetime,
    correlation_id: str,
) -> None:
    _authorize(auth, organization_id, now)

    def work(uow: AccessUnitOfWork) -> None:
        member = _target(uow, organization_id, membership_id)
        _changeable(member)
        uow.revoke_membership(membership_id, now)
        _audit(
            uow,
            auth,
            organization_id,
            "membership.revoked",
            ("membership", membership_id),
            before={"user_id": str(member.user_id), "role": member.role.value},
            after={"revoked_at": now.isoformat()},
            correlation_id=correlation_id,
        )

    await store.run(auth.claims, work)


async def grant_shop(  # noqa: PLR0913 - the request's fields plus the collaborators
    auth: AuthContext,
    store: AccessStore,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    correlation_id: str,
    new_id: Callable[[], uuid.UUID] = uuid.uuid7,
) -> None:
    _authorize(auth, organization_id, now)
    grant_id = new_id()

    def work(uow: AccessUnitOfWork) -> None:
        _target(uow, organization_id, membership_id)
        if not uow.shop_in_organization(organization_id, shop_id):
            raise ShopNotInOrganizationError("no such shop")
        if uow.grant_of(membership_id, shop_id) is not None:
            return
        uow.create_grant(grant_id, organization_id, membership_id, shop_id)
        _audit(
            uow,
            auth,
            organization_id,
            "shop_access.granted",
            ("shop_access", grant_id),
            after={"membership_id": str(membership_id), "shop_id": str(shop_id)},
            correlation_id=correlation_id,
        )

    await store.run(auth.claims, work)


async def revoke_shop(  # noqa: PLR0913 - the request's fields plus the collaborators
    auth: AuthContext,
    store: AccessStore,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    correlation_id: str,
) -> None:
    _authorize(auth, organization_id, now)

    def work(uow: AccessUnitOfWork) -> None:
        _target(uow, organization_id, membership_id)
        grant_id = uow.grant_of(membership_id, shop_id)
        if grant_id is None:
            raise GrantNotFoundError("no such grant")
        uow.delete_grant(grant_id)
        _audit(
            uow,
            auth,
            organization_id,
            "shop_access.revoked",
            ("shop_access", grant_id),
            before={"membership_id": str(membership_id), "shop_id": str(shop_id)},
            correlation_id=correlation_id,
        )

    await store.run(auth.claims, work)
