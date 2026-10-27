"""The Shop Environment Registry on Postgres (slice 5.16): `app.shop_browser_environments` and
its append-only history `app.browser_environment_events`.

Every write runs in one transaction with its history row and its
`browser.environment_status_changed.v1` outbox message. A status change is a compare-and-set
(`where status = <from>`), so two writers that read the same status cannot both move it; the
database's trigger refuses any edge outside the transition table either way.
"""

import uuid
from collections.abc import Mapping
from typing import Any

import psycopg

from htco_application.kernel import OutboxMessage
from htco_domain.actors import ActorRef
from htco_domain.browser import (
    BrowserEnvironment,
    ConsentRecord,
    EnvironmentChange,
    EnvironmentStatus,
    HealthReport,
)
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_COLUMNS = (
    "id, organization_id, shop_id, status, consent_text_version, consent_user_id, consent_at, "
    "consent_ip_hash, challenge_count, last_health_at, last_health_ok"
)


def _environment(row: tuple[Any, ...]) -> BrowserEnvironment:
    consent = None
    if row[4] is not None:
        consent = ConsentRecord(text_version=row[4], user_id=row[5], at=row[6], ip_hash=row[7])
    return BrowserEnvironment(
        id=row[0], organization_id=row[1], shop_id=row[2], status=EnvironmentStatus(row[3]),
        consent=consent, challenge_count=row[8], last_health_at=row[9], last_health_ok=row[10],
    )  # fmt: skip


def _add_change(connection: psycopg.Connection[Any], change: EnvironmentChange) -> None:
    connection.execute(
        """
        insert into app.browser_environment_events
          (id, organization_id, shop_id, environment_id, kind, from_status, to_status,
           actor_kind, actor_id, correlation_id, reason, occurred_at)
        values (%s, %s, %s, %s, 'transition', %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            uuid.uuid7(),
            change.organization_id,
            change.shop_id,
            change.environment_id,
            change.from_status.value,
            change.to_status.value,
            change.actor.kind.value,
            change.actor.id,
            change.correlation_id,
            change.reason,
            change.occurred_at,
        ],
    )


class PostgresEnvironmentStore:
    """The registry's store, on the API's login role; it also answers which organization owns
    a shop."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "select organization_id from app.shops where id = %s", [shop_id]
            ).fetchone()
        return None if row is None else row[0]

    def by_shop(self, shop_id: uuid.UUID) -> BrowserEnvironment | None:
        return self._one("shop_id", shop_id)

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None:
        return self._one("id", environment_id)

    def _one(self, column: str, value: uuid.UUID) -> BrowserEnvironment | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_COLUMNS} from app.shop_browser_environments where {column} = %s",  # noqa: S608 - constant names
                [value],
            ).fetchone()
        return None if row is None else _environment(row)

    def insert(
        self, environment: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        consent = environment.consent
        with self._database.connection() as connection, connection.transaction():
            created = connection.execute(
                """
                insert into app.shop_browser_environments
                  (id, organization_id, shop_id, status, consent_text_version, consent_user_id,
                   consent_at, consent_ip_hash)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (shop_id) do nothing
                returning id
                """,
                [
                    environment.id,
                    environment.organization_id,
                    environment.shop_id,
                    environment.status.value,
                    None if consent is None else consent.text_version,
                    None if consent is None else consent.user_id,
                    None if consent is None else consent.at,
                    None if consent is None else consent.ip_hash,
                ],
            ).fetchone()
            if created is None:
                return False
            _add_change(connection, change)
            add_outbox_in(connection, event, now=change.occurred_at)
        return True

    def apply(
        self, after: BrowserEnvironment, change: EnvironmentChange, event: OutboxMessage
    ) -> bool:
        consent = after.consent
        with self._database.connection() as connection, connection.transaction():
            moved = connection.execute(
                """
                update app.shop_browser_environments
                set status = %s,
                    consent_text_version = %s, consent_user_id = %s, consent_at = %s,
                    consent_ip_hash = %s
                where id = %s and status = %s
                returning id
                """,
                [
                    after.status.value,
                    None if consent is None else consent.text_version,
                    None if consent is None else consent.user_id,
                    None if consent is None else consent.at,
                    None if consent is None else consent.ip_hash,
                    after.id,
                    change.from_status.value,
                ],
            ).fetchone()
            if moved is None:
                return False
            _add_change(connection, change)
            add_outbox_in(connection, event, now=change.occurred_at)
        return True

    def add_health(
        self,
        environment: BrowserEnvironment,
        report: HealthReport,
        *,
        by: ActorRef,
        correlation_id: str,
    ) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                update app.shop_browser_environments
                set last_health_at = %s, last_health_ok = %s
                where id = %s
                """,
                [report.checked_at, report.ok, environment.id],
            )
            connection.execute(
                """
                insert into app.browser_environment_events
                  (id, organization_id, shop_id, environment_id, kind, health_ok, problems,
                   actor_kind, actor_id, correlation_id, occurred_at)
                values (%s, %s, %s, %s, 'health', %s, %s, %s, %s, %s, %s)
                """,
                [
                    uuid.uuid7(),
                    environment.organization_id,
                    environment.shop_id,
                    environment.id,
                    report.ok,
                    list(report.problems),
                    by.kind.value,
                    by.id,
                    correlation_id,
                    report.checked_at,
                ],
            )


class PostgresEnvironmentReader:
    """The status as the signed-in user sees it: RLS shows the Owner and Admins their
    organization's environments only."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def status_for_user(
        self, claims: Mapping[str, Any], shop_id: uuid.UUID
    ) -> EnvironmentStatus | None:
        with self._database.user_transaction(claims) as connection:
            row = connection.execute(
                "select status from app.shop_browser_environments where shop_id = %s",
                [shop_id],
            ).fetchone()
        return None if row is None else EnvironmentStatus(row[0])
