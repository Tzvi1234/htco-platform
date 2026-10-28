"""The audit writer (slice 2.5): appends to `audit.audit_log` inside the caller's transaction;
and the audit view's reader (AC-3.9.1), as the caller."""

from collections.abc import Mapping
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from htco_application.audit import AuditRecord
from htco_application.audit_log import AuditEntry, AuditPage, AuditQuery
from htco_persistence.database import Database

_INSERT = """
insert into audit.audit_log
  (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id, before, after,
   correlation_id)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def append_audit(connection: psycopg.Connection[Any], record: AuditRecord) -> None:
    connection.execute(
        _INSERT,
        [
            record.id,
            record.organization_id,
            record.actor.kind.value,
            record.actor.id,
            record.action,
            record.entity_type,
            record.entity_id,
            None if record.before is None else Jsonb(dict(record.before)),
            None if record.after is None else Jsonb(dict(record.after)),
            record.correlation_id,
        ],
    )


# Read as the caller: the table's policy lets only the Owner and Admins see rows (RLS), on top of
# the organization filter. IDs are UUIDv7, so `id` orders by time and is the cursor.
_LIST = """
select id, occurred_at, actor_kind, actor_id, action, entity_type, entity_id, before, after,
       correlation_id
from audit.audit_log
where organization_id = %(organization_id)s
  and (%(shop)s::text is null
       or (entity_type = 'shop' and entity_id = %(shop)s::text)
       or before ->> 'shop_id' = %(shop)s::text
       or after ->> 'shop_id' = %(shop)s::text)
  and (%(actor_kind)s::text is null or actor_kind = %(actor_kind)s::text)
  and (%(actor_id)s::text is null or actor_id = %(actor_id)s::text)
  and (%(action)s::text is null or action = %(action)s::text)
  and (%(action_prefix)s::text is null or starts_with(action, %(action_prefix)s::text))
  and (%(since)s::timestamptz is null or occurred_at >= %(since)s::timestamptz)
  and (%(until)s::timestamptz is null or occurred_at < %(until)s::timestamptz)
  and (%(before)s::uuid is null or id < %(before)s::uuid)
order by id desc
limit %(limit)s
"""


class PostgresAuditLogReader:
    """The audit view (AC-3.9.1): one page of the organization's records, newest first."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_audit(self, claims: Mapping[str, Any], query: AuditQuery) -> AuditPage:
        def run() -> AuditPage:
            action: str | None = query.action
            prefix: str | None = None
            if query.action is not None and query.action.endswith("*"):
                action, prefix = None, query.action[:-1]
            parameters = {
                "organization_id": query.organization_id,
                "shop": None if query.shop_id is None else str(query.shop_id),
                "actor_kind": None if query.actor_kind is None else query.actor_kind.value,
                "actor_id": query.actor_id,
                "action": action,
                "action_prefix": prefix,
                "since": query.since,
                "until": query.until,
                "before": query.before,
                "limit": query.limit + 1,
            }
            with self._database.user_transaction(claims) as connection:
                rows = connection.execute(_LIST, parameters).fetchall()
            items = tuple(
                AuditEntry(
                    id=r[0], occurred_at=r[1], actor_kind=r[2], actor_id=r[3], action=r[4],
                    entity_type=r[5], entity_id=r[6], before=r[7], after=r[8], correlation_id=r[9],
                )
                for r in rows[: query.limit]
            )  # fmt: skip
            more = len(rows) > query.limit
            return AuditPage(items=items, next_before=items[-1].id if more else None)

        return await anyio.to_thread.run_sync(run)


class PostgresAuditLog:
    """Appends one record in its own transaction: for events that change nothing else, such as a
    rejected OAuth callback (slice 4.3). Runs with the API's login role."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append(self, record: AuditRecord) -> None:
        def run() -> None:
            with self._database.connection() as connection:
                append_audit(connection, record)

        await anyio.to_thread.run_sync(run)
