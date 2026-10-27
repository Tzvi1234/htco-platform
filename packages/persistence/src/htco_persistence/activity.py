"""The activity writer (slice 7.7) and reader (slice 7.8) of `app.activity_records`.

`append_activity` takes the caller's connection, so a state change and its record commit
together. `PostgresActivityLog` appends one entry in its own transaction, for operations that
change nothing else in Postgres. Both refuse anything but an `ActivityEntry`, whose summary only
`summarize` can make.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import anyio
import psycopg

from htco_application.activity import ActivityEntry
from htco_application.activity_query import ActivityCursor, ActivityFilter
from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.database import Database

_INSERT = """
insert into app.activity_records
  (id, organization_id, shop_id, kind, actor_kind, actor_id, action, outcome, summary_template,
   summary, occurred_at, correlation_id, causation_id, task_id)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def append_activity(connection: psycopg.Connection[Any], entry: ActivityEntry) -> None:
    if not isinstance(entry, ActivityEntry):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("append_activity takes an ActivityEntry from new_activity()")
    r = entry.record
    connection.execute(
        _INSERT,
        [
            r.id, r.organization_id, r.shop_id, r.kind.value, r.actor.kind.value, r.actor.id,
            r.action, r.outcome.value, entry.summary.template, r.summary, r.occurred_at,
            r.correlation_id, r.causation_id, r.task_id,
        ],
    )  # fmt: skip


class PostgresActivityLog:
    def __init__(self, database: Database) -> None:
        self._database = database

    def append_activity(self, entry: ActivityEntry) -> None:
        with self._database.connection() as connection:
            append_activity(connection, entry)

    async def append(self, entry: ActivityEntry) -> None:
        await anyio.to_thread.run_sync(lambda: self.append_activity(entry))


_COLUMNS = (
    "id, kind, organization_id, shop_id, actor_kind, actor_id, action, outcome, summary,"
    " occurred_at, correlation_id, causation_id, task_id"
)
# A shop-scoped role's grants: its shops' records, never organization-wide ones (as the RLS
# policy of slice 7.7).
_SHOP_SCOPE = "(%(shops)s::uuid[] is null or shop_id = any(%(shops)s::uuid[]))"


def _record(row: tuple[Any, ...]) -> ActivityRecord:
    return ActivityRecord(
        id=row[0], kind=ActivityKind(row[1]), organization_id=row[2], shop_id=row[3],
        actor=ActorRef(ActorKind(row[4]), row[5]), action=row[6], outcome=ActivityOutcome(row[7]),
        summary=row[8], occurred_at=row[9], correlation_id=row[10], causation_id=row[11],
        task_id=row[12],
    )  # fmt: skip


class PostgresActivityReader:
    """The activity query's reads (the API's pool, organization filter in every query; the use
    case decides the shop scope by role)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def list_activity(
        self,
        organization_id: uuid.UUID,
        *,
        where: ActivityFilter,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
        before: ActivityCursor | None = None,
    ) -> tuple[ActivityRecord, ...]:
        params: dict[str, Any] = {
            "org": organization_id,
            "shops": None if shop_ids is None else list(shop_ids),
            "actor_kind": None if where.actor_kind is None else where.actor_kind.value,
            "actor_ref_kind": None if where.actor is None else where.actor.kind.value,
            "actor_id": None if where.actor is None else where.actor.id,
            "shop": where.shop_id,
            "task": where.task_id,
            "kind": None if where.kind is None else where.kind.value,
            "outcome": None if where.outcome is None else where.outcome.value,
            "since": where.since,
            "until": where.until,
            "before_at": None if before is None else before.occurred_at,
            "before_id": None if before is None else before.id,
            "limit": limit,
        }
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_COLUMNS} from app.activity_records
                where organization_id = %(org)s and {_SHOP_SCOPE}
                  and (%(actor_kind)s::text is null or actor_kind = %(actor_kind)s::text)
                  and (%(actor_id)s::text is null
                       or (actor_kind = %(actor_ref_kind)s::text and actor_id = %(actor_id)s::text))
                  and (%(shop)s::uuid is null or shop_id = %(shop)s::uuid)
                  and (%(task)s::uuid is null or task_id = %(task)s::uuid)
                  and (%(kind)s::text is null or kind = %(kind)s::text)
                  and (%(outcome)s::text is null or outcome = %(outcome)s::text)
                  and (%(since)s::timestamptz is null or occurred_at >= %(since)s::timestamptz)
                  and (%(until)s::timestamptz is null or occurred_at < %(until)s::timestamptz)
                  and (%(before_at)s::timestamptz is null
                       or (occurred_at, id) < (%(before_at)s::timestamptz, %(before_id)s::uuid))
                order by occurred_at desc, id desc
                limit %(limit)s
                """,  # noqa: S608 - constant column list and scope
                params,
            ).fetchall()
        return tuple(_record(r) for r in rows)

    def task_trail(
        self,
        organization_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
    ) -> Sequence[ActivityRecord]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_COLUMNS} from app.activity_records
                where organization_id = %(org)s and {_SHOP_SCOPE}
                  and (task_id = %(task)s or correlation_id in (
                    select t.correlation_id from app.activity_records t
                    where t.organization_id = %(org)s and t.task_id = %(task)s))
                order by occurred_at, id
                limit %(limit)s
                """,  # noqa: S608 - constant column list and scope
                {
                    "org": organization_id,
                    "task": task_id,
                    "shops": None if shop_ids is None else list(shop_ids),
                    "limit": limit,
                },
            ).fetchall()
        return tuple(_record(r) for r in rows)
