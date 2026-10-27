"""Inbox and outbox on Postgres (slice 5.2): `jobs.inbox` and `jobs.outbox`.

`record` and `add_outbox_in` write the row and its job in the same transaction (`enqueue_in`),
so the event or the effect can never exist without the job that handles it, nor the job without
its row.
"""

import uuid
from datetime import datetime
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from htco_application.jobs import JobSpec
from htco_application.kernel import OutboxMessage
from htco_application.messaging import (
    InboxEvent,
    PendingDelivery,
    RecordedEvent,
    delivery_job,
    payload_hash,
)
from htco_persistence.database import Database
from htco_persistence.jobs import enqueue_in


class PostgresInbox:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, event: InboxEvent, job: JobSpec, *, now: datetime) -> RecordedEvent:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                insert into jobs.inbox
                  (id, source, event_id, event_type, organization_id, shop_id, payload,
                   payload_hash, received_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (source, event_id) do nothing
                returning id
                """,
                [
                    uuid.uuid7(),
                    event.source,
                    event.event_id,
                    event.event_type,
                    event.organization_id,
                    event.shop_id,
                    Jsonb(dict(event.payload)),
                    payload_hash(event.payload),
                    event.received_at,
                ],
            ).fetchone()
            if row is None:
                first = connection.execute(
                    "select id, job_id from jobs.inbox where source = %s and event_id = %s",
                    [event.source, event.event_id],
                ).fetchone()
                if first is None or first[1] is None:  # pragma: no cover - written together
                    raise RuntimeError("an inbox row exists without its job")
                return RecordedEvent(inbox_id=first[0], job_id=first[1], duplicate=True)
            inbox_id: uuid.UUID = row[0]
            job_id = enqueue_in(connection, job, now=now)
            connection.execute(
                "update jobs.inbox set job_id = %s where id = %s", [job_id, inbox_id]
            )
        return RecordedEvent(inbox_id=inbox_id, job_id=job_id, duplicate=False)


def add_outbox_in(
    connection: psycopg.Connection[Any], message: OutboxMessage, *, now: datetime
) -> uuid.UUID:
    """Write the effect's intent and its delivery job inside the business transaction. A message
    whose key exists already is not written again; the first one's ID is returned."""
    row = connection.execute(
        """
        insert into jobs.outbox
          (id, organization_id, shop_id, destination, idempotency_key, payload, next_attempt_at,
           action_execution_id, correlation_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (idempotency_key) do nothing
        returning id
        """,
        [
            uuid.uuid7(),
            message.organization_id,
            message.shop_id,
            message.destination,
            message.idempotency_key,
            Jsonb(dict(message.payload)),
            now,
            message.action_execution_id,
            message.correlation_id,
        ],
    ).fetchone()
    if row is None:
        first = connection.execute(
            "select id from jobs.outbox where idempotency_key = %s", [message.idempotency_key]
        ).fetchone()
        if first is None:  # pragma: no cover - the conflict means it exists
            raise RuntimeError("the outbox message disappeared")
        existing: uuid.UUID = first[0]
        return existing
    message_id: uuid.UUID = row[0]
    enqueue_in(connection, delivery_job(message_id, message), now=now)
    return message_id


class PostgresOutbox:
    def __init__(self, database: Database) -> None:
        self._database = database

    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID:
        with self._database.connection() as connection, connection.transaction():
            return add_outbox_in(connection, message, now=now)

    async def load(self, message_id: uuid.UUID) -> PendingDelivery | None:
        def run() -> PendingDelivery | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    select id, organization_id, shop_id, destination, idempotency_key, payload,
                           attempts, status, action_execution_id, correlation_id
                    from jobs.outbox where id = %s
                    """,
                    [message_id],
                ).fetchone()
            if row is None:
                return None
            return PendingDelivery(
                id=row[0],
                message=OutboxMessage(
                    organization_id=row[1],
                    shop_id=row[2],
                    destination=row[3],
                    idempotency_key=row[4],
                    payload=row[5],
                    action_execution_id=row[8],
                    correlation_id=row[9],
                ),
                attempts=row[6],
                delivered=row[7] == "delivered",
            )

        return await anyio.to_thread.run_sync(run)

    async def mark_delivered(self, message_id: uuid.UUID, *, now: datetime) -> None:
        await self._execute(
            """
            update jobs.outbox set status = 'delivered', delivered_at = %s,
                                   attempts = attempts + 1, last_error = null
            where id = %s and status <> 'delivered'
            """,
            [now, message_id],
        )

    async def record_failure(
        self,
        message_id: uuid.UUID,
        *,
        error: str,
        dead: bool,
        now: datetime,  # noqa: ARG002 - the port's signature; the job's retry time is stored
    ) -> None:
        # The next attempt time follows the delivery job (a trigger on `jobs.jobs`).
        await self._execute(
            """
            update jobs.outbox
            set attempts = attempts + 1, last_error = %s,
                status = case when %s then 'dead' else status end
            where id = %s and status = 'pending'
            """,
            [error, dead, message_id],
        )

    async def _execute(self, statement: str, params: list[object]) -> None:
        def run() -> None:
            with self._database.connection() as connection, connection.transaction():
                connection.execute(statement, params)

        await anyio.to_thread.run_sync(run)
