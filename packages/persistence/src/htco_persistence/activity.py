"""The activity writer (slice 7.7): appends to `app.activity_records` in the caller's transaction.

`append_activity` takes the caller's connection, so a state change and its record commit
together. `PostgresActivityLog` appends one entry in its own transaction, for operations that
change nothing else in Postgres. Both refuse anything but an `ActivityEntry`, whose summary only
`summarize` can make.
"""

from typing import Any

import anyio
import psycopg

from htco_application.activity import ActivityEntry
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
            # The summary's own rendering, never the record's copy of it (quill2 on #191).
            r.action, r.outcome.value, entry.summary.template, entry.summary.text, r.occurred_at,
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
