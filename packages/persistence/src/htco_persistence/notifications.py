"""Notifications, preferences and email deliveries on Postgres (slice 6.4), as the worker.

Synchronous; run in worker threads. Members are reached only through
`app.worker_notification_recipients` and `app.worker_member_email`, inside the job's organization.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from htco_application.messaging import OutboxMessage
from htco_application.notification_preferences import PreferenceRefusedError, PreferenceSet
from htco_application.notifications import Notification, Recipient
from htco_domain.notifications import (
    Audience,
    Locale,
    NotificationKind,
    NotificationSeverity,
    Preference,
)
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_ROLES: Mapping[Audience, list[str] | None] = {
    Audience.THE_USER: None,
    Audience.OWNERS: ["owner"],
    Audience.OWNERS_AND_ADMINS: ["owner", "admin"],
}


class PostgresNotificationStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def recipients(  # noqa: PLR0913 - the audience and what to choose by
        self,
        organization_id: uuid.UUID,
        audience: Audience,
        kind: NotificationKind,
        *,
        user_id: uuid.UUID | None,
        shop_id: uuid.UUID | None,
        severity: NotificationSeverity,
    ) -> tuple[Recipient, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select r.user_id, r.locale, p.in_app, p.email, s.in_app, s.email,
                       m.shop_id is not null
                from app.worker_notification_recipients(%(roles)s, %(user)s, %(shop)s) r
                left join app.notification_preferences p
                  on p.organization_id = %(org)s and p.user_id = r.user_id and p.kind = %(kind)s
                left join app.notification_severity_preferences s
                  on s.organization_id = %(org)s and s.user_id = r.user_id
                 and s.severity = %(severity)s
                left join app.notification_shop_mutes m
                  on m.organization_id = %(org)s and m.user_id = r.user_id
                 and m.shop_id = %(shop)s
                order by r.user_id
                """,
                {
                    "roles": _ROLES[audience],
                    "user": user_id if audience is Audience.THE_USER else None,
                    "shop": shop_id,
                    "org": organization_id,
                    "kind": kind.value,
                    "severity": severity.value,
                },
            ).fetchall()
        return tuple(
            Recipient(
                user_id=r[0],
                locale=Locale(r[1]),
                preference=None if r[2] is None else Preference(in_app=r[2], email=r[3]),
                severity_preference=None if r[4] is None else Preference(in_app=r[4], email=r[5]),
                shop_muted=bool(r[6]),
            )
            for r in rows
        )

    def add_notifications(
        self,
        notifications: tuple[Notification, ...],
        emails: Mapping[uuid.UUID, OutboxMessage],
        *,
        now: datetime,
    ) -> int:
        created = 0
        with self._database.connection() as connection, connection.transaction():
            for n in notifications:
                row = connection.execute(
                    """
                    insert into app.notifications
                      (id, organization_id, user_id, kind, params, dedup_key, shop_id, incident_id,
                       created_at, in_app)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (organization_id, user_id, dedup_key) do nothing
                    returning id
                    """,
                    [
                        n.id,
                        n.organization_id,
                        n.user_id,
                        n.kind.value,
                        Jsonb(dict(n.params)),
                        n.dedup_key,
                        n.shop_id,
                        n.incident_id,
                        n.created_at,
                        n.in_app,
                    ],
                ).fetchone()
                if row is None:
                    continue
                created += 1
                email = emails.get(n.user_id)
                if email is not None:
                    add_outbox_in(connection, email, now=now)
        return created


class PostgresEmailDirectory:
    def __init__(self, database: Database) -> None:
        self._database = database

    def address_of(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> str | None:  # noqa: ARG002 - the job's organization scopes the function
        with self._database.connection() as connection:
            row = connection.execute("select app.worker_member_email(%s)", [user_id]).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def record_email(  # noqa: PLR0913 - one delivery's facts
        self,
        *,
        outbox_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: NotificationKind,
        status: str,
        provider_id: str | None,
        error: str | None,
        now: datetime,
    ) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                insert into app.email_deliveries
                  (id, outbox_id, organization_id, user_id, kind, status, provider_id, error,
                   created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (outbox_id) do nothing
                """,
                [
                    uuid.uuid7(),
                    outbox_id,
                    organization_id,
                    user_id,
                    kind.value,
                    status,
                    provider_id,
                    error,
                    now,
                ],
            )


_NOTIFICATION_COLUMNS = """
  id, organization_id, user_id, kind, params, dedup_key, created_at, shop_id, incident_id, read_at
"""


class PostgresNotificationInbox:
    """Each member's own notifications (the API's pool; the user filter in every query)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def inbox(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool, limit: int
    ) -> tuple[list[Notification], int]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_NOTIFICATION_COLUMNS} from app.notifications
                where organization_id = %s and user_id = %s and in_app
                  and (not %s or read_at is null)
                order by created_at desc, id desc
                limit %s
                """,  # noqa: S608 - a constant column list
                [organization_id, user_id, unread_only, limit],
            ).fetchall()
            unread = connection.execute(
                """
                select count(*) from app.notifications
                where organization_id = %s and user_id = %s and in_app and read_at is null
                """,
                [organization_id, user_id],
            ).fetchone()
        items = [
            Notification(
                id=r[0], organization_id=r[1], user_id=r[2], kind=NotificationKind(r[3]),
                params={str(k): str(v) for k, v in r[4].items()}, dedup_key=r[5],
                created_at=r[6], shop_id=r[7], incident_id=r[8], read_at=r[9],
            )
            for r in rows
        ]  # fmt: skip
        return items, 0 if unread is None else int(unread[0])

    def mark_read(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        notification_ids: Sequence[uuid.UUID] | None,
        *,
        now: datetime,
    ) -> int:
        ids = None if notification_ids is None else list(notification_ids)
        with self._database.connection() as connection, connection.transaction():
            cursor = connection.execute(
                """
                update app.notifications set read_at = %s
                where organization_id = %s and user_id = %s and read_at is null
                  and (%s::uuid[] is null or id = any(%s::uuid[]))
                """,
                [now, organization_id, user_id, ids, ids],
            )
            return cursor.rowcount


class PostgresSecurityNoticeStore:
    """The API process's side of the security notices (AC-6.4.4)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def organizations_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select organization_id from app.memberships
                where user_id = %s and revoked_at is null order by organization_id
                """,
                [user_id],
            ).fetchall()
        return tuple(r[0] for r in rows)

    def earlier_agents(self, user_id: uuid.UUID, *, before: uuid.UUID) -> frozenset[str | None]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "select distinct user_agent from private.web_sessions"
                " where user_id = %s and id <> %s",
                [user_id, before],
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID:
        with self._database.connection() as connection, connection.transaction():
            return add_outbox_in(connection, message, now=now)


class PostgresNotificationPreferences:
    """Each member's own preferences (slice 7.6; the API's pool, the user in every query)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def read(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> PreferenceSet:
        key = [organization_id, user_id]
        with self._database.connection() as connection:
            kinds = connection.execute(
                "select kind, in_app, email from app.notification_preferences"
                " where organization_id = %s and user_id = %s",
                key,
            ).fetchall()
            severities = connection.execute(
                "select severity, in_app, email from app.notification_severity_preferences"
                " where organization_id = %s and user_id = %s",
                key,
            ).fetchall()
            shops = connection.execute(
                "select shop_id from app.notification_shop_mutes"
                " where organization_id = %s and user_id = %s",
                key,
            ).fetchall()
        known = {k.value for k in NotificationKind}
        return PreferenceSet(
            kinds={
                NotificationKind(r[0]): Preference(in_app=r[1], email=r[2])
                for r in kinds
                if r[0] in known  # a kind retired since stays stored and unused
            },
            severities={
                NotificationSeverity(r[0]): Preference(in_app=r[1], email=r[2]) for r in severities
            },
            muted_shops=frozenset(r[0] for r in shops),
        )

    def replace(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        preferences: PreferenceSet,
        *,
        now: datetime,
    ) -> None:
        key = [organization_id, user_id]
        try:
            with self._database.connection() as connection, connection.transaction():
                for table in (
                    "app.notification_preferences",
                    "app.notification_severity_preferences",
                    "app.notification_shop_mutes",
                ):
                    connection.execute(
                        f"delete from {table} where organization_id = %s and user_id = %s",  # noqa: S608 - a constant table
                        key,
                    )
                for kind, preference in preferences.kinds.items():
                    connection.execute(
                        "insert into app.notification_preferences"
                        " (organization_id, user_id, kind, in_app, email, updated_at)"
                        " values (%s, %s, %s, %s, %s, %s)",
                        [*key, kind.value, preference.in_app, preference.email, now],
                    )
                for severity, preference in preferences.severities.items():
                    connection.execute(
                        "insert into app.notification_severity_preferences"
                        " (organization_id, user_id, severity, in_app, email, updated_at)"
                        " values (%s, %s, %s, %s, %s, %s)",
                        [*key, severity.value, preference.in_app, preference.email, now],
                    )
                for shop_id in sorted(preferences.muted_shops):
                    connection.execute(
                        "insert into app.notification_shop_mutes"
                        " (organization_id, user_id, shop_id, created_at) values (%s, %s, %s, %s)",
                        [*key, shop_id, now],
                    )
        except psycopg.errors.ForeignKeyViolation:
            raise PreferenceRefusedError("that shop is not in this organization") from None
