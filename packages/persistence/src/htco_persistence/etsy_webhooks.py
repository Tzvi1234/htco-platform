"""Etsy webhooks on Postgres (slice 5.3): the shop a webhook names, events kept with no job, and
the inbox's processed mark.

`PostgresWebhookShops` runs in the API (its login role, like the token vault): it maps Etsy's
shop ID to the one connected HTCO shop. `PostgresWebhookInbox` extends the 5.2 inbox; its
`record` is the inbox's own. `PostgresWorkerShops` and `mark_processed` run on the worker's pool
(`htco_worker`) inside the job's organization.
"""

import uuid
from datetime import datetime

import anyio
from psycopg.types.json import Jsonb

from htco_application.kernel import TenantShop
from htco_application.messaging import InboxEvent, payload_hash
from htco_persistence.database import Database
from htco_persistence.messaging import PostgresInbox


class PostgresWebhookShops:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def connected_shop(self, etsy_shop_id: int) -> TenantShop | None:
        """The HTCO shop connected to `etsy_shop_id`; None when there is none, or more than one
        (an ambiguous webhook is stored, never routed to a guess)."""

        def run() -> TenantShop | None:
            with self._database.connection() as connection:
                rows = connection.execute(
                    """
                    select organization_id, shop_id from app.shop_integrations
                    where etsy_shop_id = %s and status = 'connected'
                    limit 2
                    """,
                    [etsy_shop_id],
                ).fetchall()
            if len(rows) != 1:
                return None
            return TenantShop(organization_id=rows[0][0], shop_id=rows[0][1])

        return await anyio.to_thread.run_sync(run)


class PostgresWebhookInbox(PostgresInbox):
    def keep(self, event: InboxEvent, *, now: datetime) -> tuple[uuid.UUID, bool]:  # noqa: ARG002 - the port's clock; `received_at` is the event's
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
            if row is not None:
                created: uuid.UUID = row[0]
                return created, False
            first = connection.execute(
                "select id from jobs.inbox where source = %s and event_id = %s",
                [event.source, event.event_id],
            ).fetchone()
        if first is None:  # pragma: no cover - the conflict means it exists
            raise RuntimeError("the inbox event disappeared")
        existing: uuid.UUID = first[0]
        return existing, True

    def mark_processed(self, job_id: uuid.UUID, *, now: datetime) -> int:
        with self._database.connection() as connection, connection.transaction():
            cursor = connection.execute(
                """
                update jobs.inbox set processed_at = %s
                where job_id = %s and processed_at is null
                """,
                [now, job_id],
            )
            return cursor.rowcount


class PostgresWorkerShops:
    """The Etsy shop behind an HTCO shop, read as `htco_worker` in the job's organization."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def etsy_shop_of(self, shop: TenantShop) -> int | None:
        def run() -> int | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    """
                    select etsy_shop_id from app.shop_integrations
                    where organization_id = %s and shop_id = %s and status = 'connected'
                    """,
                    [shop.organization_id, shop.shop_id],
                ).fetchone()
            return None if row is None else int(row[0])

        return await anyio.to_thread.run_sync(run)
