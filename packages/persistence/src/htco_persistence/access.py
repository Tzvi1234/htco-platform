"""Membership and shop-access changes (slice 2.6), run as the calling user.

Each unit of work is one user-scoped transaction, so the RLS policies of slice 2.2 apply on top of
the application's checks: writes to memberships and shop grants need Owner or Admin of the
organization and an `aal2` session. A write that RLS silently filters out (zero rows) raises
instead of passing as a success.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import anyio
import psycopg

from htco_application.access.management import AccessDeniedError, AccessUnitOfWork, MemberRow
from htco_application.audit import AuditRecord
from htco_domain.identity import Role
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

_MEMBERS = """
select m.id, m.organization_id, m.user_id, m.role, p.display_name,
       coalesce(array_agg(sa.shop_id) filter (where sa.shop_id is not null), '{}') as shop_ids
from app.memberships m
left join app.user_profiles p on p.user_id = m.user_id
left join app.shop_access sa on sa.membership_id = m.id
where m.organization_id = %(organization_id)s
  and m.revoked_at is null
  and (%(membership_id)s::uuid is null or m.id = %(membership_id)s::uuid)
group by m.id, p.display_name
order by m.created_at, m.id
"""


def _row(row: tuple[Any, ...]) -> MemberRow:
    return MemberRow(
        id=row[0],
        organization_id=row[1],
        user_id=row[2],
        role=Role(row[3]),
        display_name=row[4],
        shop_ids=frozenset(row[5]),
    )


class _PostgresAccessWork:
    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def _changed_one(self, sql: str, params: Sequence[object]) -> None:
        if self._connection.execute(sql, params).rowcount != 1:
            raise AccessDeniedError("the database refused the change")

    def member(self, organization_id: uuid.UUID, membership_id: uuid.UUID) -> MemberRow | None:
        row = self._connection.execute(
            _MEMBERS, {"organization_id": organization_id, "membership_id": membership_id}
        ).fetchone()
        return None if row is None else _row(row)

    def members_of(self, organization_id: uuid.UUID) -> Sequence[MemberRow]:
        rows = self._connection.execute(
            _MEMBERS, {"organization_id": organization_id, "membership_id": None}
        ).fetchall()
        return [_row(row) for row in rows]

    def active_member_by_user(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        row = self._connection.execute(
            """
            select exists (
              select 1 from app.memberships
              where organization_id = %s and user_id = %s and revoked_at is null
            )
            """,
            [organization_id, user_id],
        ).fetchone()
        return bool(row and row[0])

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None:
        self._connection.execute(
            """
            insert into app.memberships (id, organization_id, user_id, role)
            values (%s, %s, %s, %s)
            """,
            [membership_id, organization_id, user_id, role.value],
        )

    def update_role(self, membership_id: uuid.UUID, role: Role) -> None:
        self._changed_one(
            "update app.memberships set role = %s where id = %s and revoked_at is null",
            [role.value, membership_id],
        )

    def revoke_membership(self, membership_id: uuid.UUID, at: datetime) -> None:
        self._changed_one(
            "update app.memberships set revoked_at = %s where id = %s and revoked_at is null",
            [at, membership_id],
        )

    def shop_in_organization(self, organization_id: uuid.UUID, shop_id: uuid.UUID) -> bool:
        row = self._connection.execute(
            "select exists (select 1 from app.shops where id = %s and organization_id = %s)",
            [shop_id, organization_id],
        ).fetchone()
        return bool(row and row[0])

    def grant_of(self, membership_id: uuid.UUID, shop_id: uuid.UUID) -> uuid.UUID | None:
        row = self._connection.execute(
            "select id from app.shop_access where membership_id = %s and shop_id = %s",
            [membership_id, shop_id],
        ).fetchone()
        return None if row is None else row[0]

    def create_grant(
        self,
        grant_id: uuid.UUID,
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        shop_id: uuid.UUID,
    ) -> None:
        self._connection.execute(
            """
            insert into app.shop_access (id, organization_id, membership_id, shop_id)
            values (%s, %s, %s, %s)
            """,
            [grant_id, organization_id, membership_id, shop_id],
        )

    def delete_grant(self, grant_id: uuid.UUID) -> None:
        self._changed_one("delete from app.shop_access where id = %s", [grant_id])

    def append_audit(self, record: AuditRecord) -> None:
        append_audit(self._connection, record)


class PostgresAccessStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def run[T](self, claims: Mapping[str, Any], work: Callable[[AccessUnitOfWork], T]) -> T:
        return await anyio.to_thread.run_sync(self._run, claims, work)

    def _run[T](self, claims: Mapping[str, Any], work: Callable[[AccessUnitOfWork], T]) -> T:
        with self._database.user_transaction(claims) as connection:
            return work(_PostgresAccessWork(connection))
