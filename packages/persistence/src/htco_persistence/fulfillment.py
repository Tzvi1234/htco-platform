"""Tracking events, SLA policies and incidents on Postgres (slices 6.1 to 6.3).

Synchronous like the other worker stores; run in worker threads. Every write that has an event
writes it in the same transaction (`add_outbox_in`). Nothing here reads a buyer column.
"""

import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from htco_application.incident_center import IncidentDetail, IncidentHistoryEntry
from htco_application.incidents import IncidentRecord, IncidentStep, StaleIncident
from htco_application.messaging import OutboxMessage
from htco_application.sla_inspector import AwaitingOrder
from htco_application.tracking_sync import RecordedTracking, TrackingObservation, TrackingSource
from htco_domain.fulfillment import BusinessCalendar, SlaPolicy, WindowUnit
from htco_domain.incidents import (
    Cause,
    Evidence,
    EvidenceKind,
    IncidentState,
    ResourceKind,
    Severity,
)
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in


class PostgresTrackingStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record_tracking(
        self, observation: TrackingObservation, event: OutboxMessage, *, now: datetime
    ) -> RecordedTracking:
        o = observation
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                insert into app.tracking_events
                  (id, organization_id, shop_id, receipt_id, carrier, carrier_name, fingerprint,
                   last4, source, observed_via, first_observed_at, notified_at, execution_id,
                   correlation_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (shop_id, receipt_id, fingerprint) do nothing
                returning id
                """,
                [
                    o.id,
                    o.organization_id,
                    o.shop_id,
                    o.receipt_id,
                    o.carrier,
                    o.carrier_name,
                    o.fingerprint,
                    o.last4,
                    o.source.value,
                    o.observed_via.value,
                    o.observed_at,
                    o.notified_at,
                    o.execution_id,
                    o.correlation_id,
                ],
            ).fetchone()
            if row is not None:
                add_outbox_in(connection, event, now=now)
                return RecordedTracking(o.id, True, o.observed_at, o.source)
            first = connection.execute(
                """
                select id, first_observed_at, source from app.tracking_events
                where shop_id = %s and receipt_id = %s and fingerprint = %s
                """,
                [o.shop_id, o.receipt_id, o.fingerprint],
            ).fetchone()
        if first is None:  # pragma: no cover - the conflict means it exists
            raise RuntimeError("the tracking event disappeared")
        return RecordedTracking(first[0], False, first[1], TrackingSource(first[2]))

    def untracked_shipped(self, shop_id: uuid.UUID, *, limit: int) -> tuple[int, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select o.receipt_id from app.etsy_orders o
                where o.shop_id = %s and o.is_shipped
                  and o.order_state in ('shipped', 'delivered')
                  and not exists (
                    select 1 from app.tracking_checks c
                    where c.shop_id = o.shop_id and c.receipt_id = o.receipt_id
                      and c.etsy_updated_at >= o.etsy_updated_at
                  )
                order by o.etsy_updated_at, o.receipt_id
                limit %s
                """,
                [shop_id, limit],
            ).fetchall()
        return tuple(int(r[0]) for r in rows)

    def mark_checked(self, shop_id: uuid.UUID, receipt_id: int, *, now: datetime) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                insert into app.tracking_checks
                  (organization_id, shop_id, receipt_id, etsy_updated_at, checked_at)
                select o.organization_id, o.shop_id, o.receipt_id, o.etsy_updated_at, %s
                from app.etsy_orders o where o.shop_id = %s and o.receipt_id = %s
                on conflict (shop_id, receipt_id) do update
                  set etsy_updated_at = excluded.etsy_updated_at, checked_at = excluded.checked_at
                """,
                [now, shop_id, receipt_id],
            )


def _policy(row: tuple[Any, ...]) -> SlaPolicy:
    holidays: list[date] = row[8] or []
    return SlaPolicy(
        id=row[0],
        shop_id=row[1],
        supplier=row[2],
        shipping_method=row[3],
        window=row[4],
        unit=WindowUnit(row[5]),
        calendar=BusinessCalendar(
            timezone=row[6], weekend=frozenset(int(d) for d in row[7]), holidays=frozenset(holidays)
        ),
        version=row[9],
    )


class PostgresSlaStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def policies(self, organization_id: uuid.UUID) -> tuple[SlaPolicy, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select id, shop_id, supplier, shipping_method, window_days, unit, timezone,
                       weekend, holidays, version
                from app.sla_policies where organization_id = %s
                """,
                [organization_id],
            ).fetchall()
        return tuple(_policy(r) for r in rows)

    def awaiting_tracking(self, shop_id: uuid.UUID) -> tuple[AwaitingOrder, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select o.receipt_id, o.etsy_created_at from app.etsy_orders o
                where o.shop_id = %s and o.is_paid and not o.is_shipped
                  and o.order_state in ('ingested', 'awaiting_shipment')
                  and not exists (
                    select 1 from app.tracking_events t
                    where t.shop_id = o.shop_id and t.receipt_id = o.receipt_id
                  )
                order by o.etsy_created_at
                """,
                [shop_id],
            ).fetchall()
        return tuple(AwaitingOrder(receipt_id=int(r[0]), paid_at=r[1]) for r in rows)

    def closed_orders(
        self, shop_id: uuid.UUID, receipt_ids: tuple[int, ...]
    ) -> Mapping[int, datetime]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select receipt_id, etsy_updated_at from app.etsy_orders
                where shop_id = %s and receipt_id = any(%s)
                  and order_state in ('cancelled', 'refunded')
                """,
                [shop_id, list(receipt_ids)],
            ).fetchall()
        return {int(r[0]): r[1] for r in rows}


_INCIDENT_COLUMNS = """
  id, organization_id, dedup_key, cause, resource_kind, resource_id, severity, state,
  detected_at, updated_at, source, shop_id, receipt_id, details, resolved_at
"""


def _incident(row: tuple[Any, ...]) -> IncidentRecord:
    return IncidentRecord(
        id=row[0],
        organization_id=row[1],
        dedup_key=row[2],
        cause=Cause(row[3]),
        resource_kind=ResourceKind(row[4]),
        resource_id=row[5],
        severity=Severity(row[6]),
        state=IncidentState(row[7]),
        detected_at=row[8],
        updated_at=row[9],
        source=row[10],
        shop_id=row[11],
        receipt_id=None if row[12] is None else int(row[12]),
        details=row[13],
        resolved_at=row[14],
    )


def _insert_step(connection: psycopg.Connection[Any], step: IncidentStep) -> None:
    evidence = step.evidence
    connection.execute(
        """
        insert into app.incident_events
          (id, organization_id, incident_id, from_state, to_state, actor_kind, actor_id,
           occurred_at, evidence_kind, evidence_source, evidence_reference, evidence_observed_at,
           note, correlation_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            uuid.uuid7(),
            step.organization_id,
            step.incident_id,
            None if step.from_state is None else step.from_state.value,
            step.to_state.value,
            step.actor.kind.value,
            step.actor.id,
            step.at,
            None if evidence is None else evidence.kind.value,
            None if evidence is None else evidence.source,
            None if evidence is None else evidence.reference,
            None if evidence is None else evidence.observed_at,
            step.note,
            None if step.correlation_id is None else step.correlation_id[:200],
        ],
    )


class PostgresIncidentStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def open_incident(
        self,
        incident: IncidentRecord,
        step: IncidentStep,
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,
    ) -> tuple[IncidentRecord, bool]:
        i = incident
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                f"""
                insert into app.incidents
                  (id, organization_id, dedup_key, cause, resource_kind, resource_id, severity,
                   state, detected_at, updated_at, source, shop_id, receipt_id, details)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (organization_id, dedup_key) where state <> 'resolved' do nothing
                returning {_INCIDENT_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [
                    i.id,
                    i.organization_id,
                    i.dedup_key,
                    i.cause.value,
                    i.resource_kind.value,
                    i.resource_id,
                    i.severity.value,
                    i.state.value,
                    i.detected_at,
                    i.updated_at,
                    i.source,
                    i.shop_id,
                    i.receipt_id,
                    Jsonb(dict(i.details)),
                ],
            ).fetchone()
            if row is not None:
                _insert_step(connection, step)
                for event in events:
                    add_outbox_in(connection, event, now=now)
                return _incident(row), True
            existing = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and dedup_key = %s and state <> 'resolved'
                """,  # noqa: S608 - a constant column list
                [i.organization_id, i.dedup_key],
            ).fetchone()
        if existing is None:  # pragma: no cover - resolved between the two statements
            raise StaleIncident("the open incident was resolved meanwhile")
        return _incident(existing), False

    def open_incident_for(self, organization_id: uuid.UUID, key: str) -> IncidentRecord | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and dedup_key = %s and state <> 'resolved'
                """,  # noqa: S608 - a constant column list
                [organization_id, key],
            ).fetchone()
        return None if row is None else _incident(row)

    def get_incident(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentRecord | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and id = %s
                """,  # noqa: S608 - a constant column list
                [organization_id, incident_id],
            ).fetchone()
        return None if row is None else _incident(row)

    def move_incident(
        self,
        incident: IncidentRecord,
        steps: tuple[IncidentStep, ...],
        events: tuple[OutboxMessage, ...],
        *,
        now: datetime,
    ) -> IncidentRecord:
        if not steps:
            return incident
        final = steps[-1].to_state
        with self._database.connection() as connection, connection.transaction():
            locked = connection.execute(
                "select state from app.incidents where id = %s and organization_id = %s for update",
                [incident.id, incident.organization_id],
            ).fetchone()
            if locked is None or locked[0] != incident.state.value:
                raise StaleIncident("the incident moved meanwhile")
            for step in steps:
                _insert_step(connection, step)
            row = connection.execute(
                f"""
                update app.incidents
                set state = %s, updated_at = %s,
                    resolved_at = case when %s = 'resolved' then %s::timestamptz end
                where id = %s
                returning {_INCIDENT_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [final.value, now, final.value, now, incident.id],
            ).fetchone()
            for event in events:
                add_outbox_in(connection, event, now=now)
        if row is None:  # pragma: no cover - locked above
            raise StaleIncident("the incident disappeared")
        return _incident(row)

    def open_incidents(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID | None
    ) -> tuple[IncidentRecord, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and state <> 'resolved'
                  and (%s::uuid is null or shop_id = %s)
                order by detected_at
                """,  # noqa: S608 - a constant column list
                [organization_id, shop_id, shop_id],
            ).fetchall()
        return tuple(_incident(r) for r in rows)


class PostgresIncidentReader:
    """The incident center's reads (the API's pool, organization filter in every query; the
    use case filters shops by role)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def list_incidents(
        self, organization_id: uuid.UUID, *, open_only: bool, limit: int
    ) -> tuple[IncidentRecord, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and (not %s or state <> 'resolved')
                order by detected_at desc
                limit %s
                """,  # noqa: S608 - a constant column list
                [organization_id, open_only, limit],
            ).fetchall()
        return tuple(_incident(r) for r in rows)

    def incident_detail(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentDetail | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                select {_INCIDENT_COLUMNS} from app.incidents
                where organization_id = %s and id = %s
                """,  # noqa: S608 - a constant column list
                [organization_id, incident_id],
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                """
                select from_state, to_state, actor_kind, actor_id, occurred_at, note,
                       evidence_kind, evidence_source, evidence_reference, evidence_observed_at
                from app.incident_events where incident_id = %s
                order by occurred_at, created_at, (to_state = 'resolved')
                """,
                [incident_id],
            ).fetchall()
        return IncidentDetail(
            incident=_incident(row),
            history=tuple(
                IncidentHistoryEntry(
                    from_state=None if e[0] is None else IncidentState(e[0]),
                    to_state=IncidentState(e[1]),
                    actor_kind=e[2],
                    actor_id=e[3],
                    at=e[4],
                    note=e[5],
                    evidence=None
                    if e[6] is None
                    else Evidence(EvidenceKind(e[6]), e[9], e[7], e[8]),
                )
                for e in events
            ),
        )
