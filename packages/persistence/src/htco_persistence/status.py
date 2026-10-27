"""The chain view's reader on Postgres (slice 5.18).

Runs on the API's login role, after `execution_chain` has decided the caller may see the office
of the organization; every query filters by that organization explicitly. It reads one automation
execution, its state changes, the inbox event that caused it (when its `causation_id` is an inbox
row of the same organization), the action executions it caused (`causation_id` = the
execution's ID, the convention of slice 5.14) or that share its correlation ID, and the events
they published (`jobs.outbox`, destination `events.publish`): types and times only, never the
payloads.
"""

import uuid
from typing import Any

import psycopg

from htco_application.kernel import EVENTS_DESTINATION
from htco_application.status import (
    ActionRecord,
    ChainRecords,
    ChangeRecord,
    EventRecord,
    ExecutionRecord,
    TriggerRecord,
)
from htco_domain.actions import ActionState
from htco_domain.automation import ExecutionState
from htco_persistence.database import Database

_MAX_ROWS = 500


def _uuid(text: str | None) -> uuid.UUID | None:
    if text is None:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


class PostgresChainReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    def records(self, organization_id: uuid.UUID, execution_id: uuid.UUID) -> ChainRecords | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select id, organization_id, shop_id, automation_id, state, correlation_id,
                       created_at, causation_id, trigger, output
                from app.automation_executions where id = %s and organization_id = %s
                """,
                [execution_id, organization_id],
            ).fetchone()
            if row is None:
                return None
            execution = ExecutionRecord(
                id=row[0], organization_id=row[1], shop_id=row[2], automation_id=row[3],
                state=ExecutionState(row[4]), correlation_id=row[5], created_at=row[6],
                output=row[9],
            )  # fmt: skip
            trigger = self._trigger(connection, organization_id, row[7], row[8], row[6])
            actions = self._actions(connection, organization_id, execution)
            return ChainRecords(
                execution=execution,
                trigger=trigger,
                changes=self._changes(connection, organization_id, execution_id),
                actions=actions,
                events=self._events(connection, organization_id, execution, actions),
            )

    @staticmethod
    def _trigger(
        connection: psycopg.Connection[Any],
        organization_id: uuid.UUID,
        causation_id: str | None,
        trigger: str,
        created_at: Any,
    ) -> TriggerRecord:
        inbox_id = _uuid(causation_id)
        if inbox_id is not None:
            found = connection.execute(
                """
                select id, source, event_type, received_at from jobs.inbox
                where id = %s and organization_id = %s
                """,
                [inbox_id, organization_id],
            ).fetchone()
            if found is not None:
                return TriggerRecord(str(found[0]), "event", found[3], f"{found[1]}:{found[2]}")
        return TriggerRecord(causation_id or trigger, trigger, created_at, trigger)

    @staticmethod
    def _changes(
        connection: psycopg.Connection[Any], organization_id: uuid.UUID, execution_id: uuid.UUID
    ) -> list[ChangeRecord]:
        rows = connection.execute(
            """
            select from_state, to_state, actor_kind || ':' || actor_id, reason, occurred_at
            from app.automation_execution_events
            where execution_id = %s and organization_id = %s
            order by occurred_at, created_at limit %s
            """,
            [execution_id, organization_id, _MAX_ROWS],
        ).fetchall()
        return [
            ChangeRecord(
                None if r[0] is None else ExecutionState(r[0]),
                ExecutionState(r[1]),
                r[2],
                r[3],
                r[4],
            )
            for r in rows
        ]

    @staticmethod
    def _actions(
        connection: psycopg.Connection[Any], organization_id: uuid.UUID, execution: ExecutionRecord
    ) -> list[ActionRecord]:
        rows = connection.execute(
            """
            select id, action_name, state, outcome, channel, error_code, approval_id, created_at,
                   updated_at
            from app.action_executions
            where organization_id = %s and (causation_id = %s or correlation_id = %s)
            order by created_at limit %s
            """,
            [organization_id, str(execution.id), execution.correlation_id, _MAX_ROWS],
        ).fetchall()
        return [
            ActionRecord(
                id=r[0],
                action=r[1],
                state=ActionState(r[2]),
                outcome=r[3],
                channel=r[4],
                error_code=r[5],
                approval_id=r[6],
                created_at=r[7],
                updated_at=r[8],
            )
            for r in rows
        ]

    @staticmethod
    def _events(
        connection: psycopg.Connection[Any],
        organization_id: uuid.UUID,
        execution: ExecutionRecord,
        actions: list[ActionRecord],
    ) -> list[EventRecord]:
        rows = connection.execute(
            """
            select id, payload ->> 'event_type', action_execution_id, created_at from jobs.outbox
            where organization_id = %s and destination = %s
              and (action_execution_id = any(%s)
                   or coalesce(payload -> 'payload' ->> 'execution_id', payload ->> 'execution_id')
                      = %s)
            order by created_at limit %s
            """,
            [
                organization_id,
                EVENTS_DESTINATION,
                [a.id for a in actions],
                str(execution.id),
                _MAX_ROWS,
            ],
        ).fetchall()
        return [EventRecord(r[0], r[1] or "unknown", r[2], r[3]) for r in rows]
