"""The owner bootstrap (slice 2.5): the first organization and its owner, once.

The owner signs up through Supabase Auth first; this command then creates the organization, the
owner's profile and the owner membership in one transaction, with an audit record for each
change, written by the `system:bootstrap` actor. Running it again finds the existing owner
membership and changes nothing (AC-2.5.1), so it is safe to re-run after a failure.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from htco_application.audit import AuditRecord, AuditWriter
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role

BOOTSTRAP_ACTOR = ActorRef(ActorKind.SYSTEM, "bootstrap")
_MAX_NAME_LENGTH = 200


class UserNotFoundError(Exception):
    """No signed-up user has this email; the owner must sign up before the bootstrap."""


@dataclass(frozen=True)
class BootstrapRequest:
    email: str
    organization_name: str
    display_name: str

    def __post_init__(self) -> None:
        email = self.email.strip()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("email must be an email address")
        for name in ("organization_name", "display_name"):
            value = getattr(self, name).strip()
            if not 1 <= len(value) <= _MAX_NAME_LENGTH:
                raise ValueError(f"{name} must be 1 to {_MAX_NAME_LENGTH} characters")


@dataclass(frozen=True)
class BootstrapResult:
    created: bool
    organization_id: uuid.UUID
    membership_id: uuid.UUID


class BootstrapUnitOfWork(AuditWriter, Protocol):
    def find_user_id(self, email: str) -> uuid.UUID | None: ...

    def owner_membership_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None: ...

    def create_organization(self, organization_id: uuid.UUID, name: str) -> None: ...

    def ensure_profile(self, user_id: uuid.UUID, display_name: str) -> None: ...

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None: ...


class BootstrapStore(Protocol):
    """Runs one unit of work in a single transaction, serialized against concurrent runs."""

    async def run[T](self, work: Callable[[BootstrapUnitOfWork], T]) -> T: ...


async def bootstrap_owner(
    store: BootstrapStore,
    request: BootstrapRequest,
    *,
    new_id: Callable[[], uuid.UUID] = uuid.uuid7,
    correlation_id: str,
) -> BootstrapResult:
    def work(uow: BootstrapUnitOfWork) -> BootstrapResult:
        user_id = uow.find_user_id(request.email.strip().lower())
        if user_id is None:
            raise UserNotFoundError("no user has signed up with this email")
        existing = uow.owner_membership_of(user_id)
        if existing is not None:
            return BootstrapResult(
                created=False, organization_id=existing[0], membership_id=existing[1]
            )
        organization_id, membership_id = new_id(), new_id()
        name = request.organization_name.strip()
        uow.create_organization(organization_id, name)
        uow.ensure_profile(user_id, request.display_name.strip())
        uow.create_membership(membership_id, organization_id, user_id, Role.OWNER)
        uow.append_audit(
            AuditRecord.new(
                organization_id=organization_id,
                actor=BOOTSTRAP_ACTOR,
                action="organization.created",
                entity_type="organization",
                entity_id=str(organization_id),
                after={"name": name},
                correlation_id=correlation_id,
            )
        )
        uow.append_audit(
            AuditRecord.new(
                organization_id=organization_id,
                actor=BOOTSTRAP_ACTOR,
                action="membership.created",
                entity_type="membership",
                entity_id=str(membership_id),
                after={"user_id": str(user_id), "role": Role.OWNER.value},
                correlation_id=correlation_id,
            )
        )
        return BootstrapResult(
            created=True, organization_id=organization_id, membership_id=membership_id
        )

    return await store.run(work)
