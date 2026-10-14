"""The owner bootstrap's database side (slice 2.5).

Administrative tooling, not the request path: it runs with the login role's privileges, because
no user is signed in yet. The whole unit of work is one transaction behind an advisory lock, so
two bootstraps started together still create one organization.
"""

import uuid
from collections.abc import Callable
from typing import Any

import anyio
import psycopg

from htco_application.audit import AuditRecord
from htco_application.bootstrap import BootstrapUnitOfWork
from htco_domain.identity import Role
from htco_persistence.audit import append_audit
from htco_persistence.database import Database


class _PostgresBootstrapWork:
    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def find_user_id(self, email: str) -> uuid.UUID | None:
        row = self._connection.execute(
            "select id from auth.users where lower(email) = %s", [email]
        ).fetchone()
        return None if row is None else row[0]

    def owner_membership_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None:
        row = self._connection.execute(
            """
            select organization_id, id from app.memberships
            where user_id = %s and role = 'owner' and revoked_at is null
            order by created_at, id
            limit 1
            """,
            [user_id],
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def create_organization(self, organization_id: uuid.UUID, name: str) -> None:
        self._connection.execute(
            "insert into app.organizations (id, name) values (%s, %s)", [organization_id, name]
        )

    def ensure_profile(self, user_id: uuid.UUID, display_name: str) -> None:
        self._connection.execute(
            """
            insert into app.user_profiles (user_id, display_name) values (%s, %s)
            on conflict (user_id) do nothing
            """,
            [user_id, display_name],
        )

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

    def append_audit(self, record: AuditRecord) -> None:
        append_audit(self._connection, record)


class PostgresBootstrapStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def run[T](self, work: Callable[[BootstrapUnitOfWork], T]) -> T:
        return await anyio.to_thread.run_sync(self._run, work)

    def _run[T](self, work: Callable[[BootstrapUnitOfWork], T]) -> T:
        with self._database.connection() as connection, connection.transaction():
            connection.execute("select pg_advisory_xact_lock(hashtext('htco.bootstrap'))")
            return work(_PostgresBootstrapWork(connection))
