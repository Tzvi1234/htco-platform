"""In-memory fakes for access management, shared by the application and API tests (slice 2.6)."""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from htco_application.access.management import InviteeAlreadyRegisteredError, MemberRow
from htco_application.audit import AuditRecord
from htco_domain.identity import Role

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("b0000000-0000-0000-0000-000000000000")
OWNER = uuid.UUID("11111111-0000-0000-0000-000000000001")
ADMIN = uuid.UUID("11111111-0000-0000-0000-000000000002")
SUPPORT = uuid.UUID("11111111-0000-0000-0000-000000000003")
NEWCOMER = uuid.UUID("11111111-0000-0000-0000-000000000009")
M_OWNER = uuid.UUID("ae000000-0000-0000-0000-000000000001")
M_ADMIN = uuid.UUID("ae000000-0000-0000-0000-000000000002")
M_SUPPORT = uuid.UUID("ae000000-0000-0000-0000-000000000003")
M_ELSEWHERE = uuid.UUID("be000000-0000-0000-0000-000000000001")
SHOP = uuid.UUID("a1000000-0000-0000-0000-000000000001")
FOREIGN_SHOP = uuid.UUID("b1000000-0000-0000-0000-000000000001")


@dataclass
class Store:
    """The organization's access data in memory; one unit of work at a time, all or nothing."""

    members: dict[uuid.UUID, MemberRow] = field(default_factory=dict)
    shops: dict[uuid.UUID, uuid.UUID] = field(
        default_factory=lambda: {SHOP: ORG, FOREIGN_SHOP: OTHER_ORG}
    )
    grants: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = field(default_factory=dict)
    audit: list[AuditRecord] = field(default_factory=list)
    claims_seen: list[Mapping[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for row in (
            MemberRow(M_OWNER, ORG, OWNER, Role.OWNER, "Owner", frozenset()),
            MemberRow(M_ADMIN, ORG, ADMIN, Role.ADMIN, "Admin", frozenset()),
            MemberRow(M_SUPPORT, ORG, SUPPORT, Role.SUPPORT, None, frozenset()),
            MemberRow(M_ELSEWHERE, OTHER_ORG, NEWCOMER, Role.OWNER, None, frozenset()),
        ):
            self.members[row.id] = row

    async def run[T](self, claims: Mapping[str, Any], work: Callable[[Store], T]) -> T:
        self.claims_seen.append(claims)
        snapshot = (dict(self.members), dict(self.grants), list(self.audit))
        try:
            return work(self)
        except Exception:
            self.members, self.grants, self.audit = snapshot
            raise

    def member(self, organization_id: uuid.UUID, membership_id: uuid.UUID) -> MemberRow | None:
        row = self.members.get(membership_id)
        if row is None or row.organization_id != organization_id:
            return None
        shops = frozenset(s for (m, s) in self.grants if m == membership_id)
        return MemberRow(
            row.id, row.organization_id, row.user_id, row.role, row.display_name, shops
        )

    def members_of(self, organization_id: uuid.UUID) -> Sequence[MemberRow]:
        found = [self.member(organization_id, m) for m in sorted(self.members)]
        return [row for row in found if row is not None]

    def active_member_by_user(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return any(
            r.organization_id == organization_id and r.user_id == user_id
            for r in self.members.values()
        )

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None:
        self.members[membership_id] = MemberRow(
            membership_id, organization_id, user_id, role, None, frozenset()
        )

    def update_role(self, membership_id: uuid.UUID, role: Role) -> None:
        row = self.members[membership_id]
        self.members[membership_id] = MemberRow(
            row.id, row.organization_id, row.user_id, role, row.display_name, row.shop_ids
        )

    def revoke_membership(self, membership_id: uuid.UUID, at: datetime) -> None:
        assert at == NOW
        del self.members[membership_id]

    def shop_in_organization(self, organization_id: uuid.UUID, shop_id: uuid.UUID) -> bool:
        return self.shops.get(shop_id) == organization_id

    def grant_of(self, membership_id: uuid.UUID, shop_id: uuid.UUID) -> uuid.UUID | None:
        return self.grants.get((membership_id, shop_id))

    def create_grant(
        self,
        grant_id: uuid.UUID,
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        shop_id: uuid.UUID,
    ) -> None:
        assert self.shops[shop_id] == organization_id
        self.grants[(membership_id, shop_id)] = grant_id

    def delete_grant(self, grant_id: uuid.UUID) -> None:
        self.grants = {k: v for k, v in self.grants.items() if v != grant_id}

    def append_audit(self, record: AuditRecord) -> None:
        self.audit.append(record)


class Inviter:
    def __init__(self, user_id: uuid.UUID = NEWCOMER, *, registered: bool = False) -> None:
        self.user_id, self.registered = user_id, registered
        self.invited: list[str] = []

    async def invite(self, email: str) -> uuid.UUID:
        self.invited.append(email)
        if self.registered:
            raise InviteeAlreadyRegisteredError(email)
        return self.user_id
