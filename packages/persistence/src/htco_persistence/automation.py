"""The durable automation engine on Postgres (slice 5.10): definitions, executions, their events.

`PostgresExecutionStore` is the engine's `DurableExecutionStore`: `create` inserts the execution
with `on conflict do nothing` on `(automation_id, idempotency_key)`, so concurrent triggers have
one winner and every caller gets the same row; `advance` updates only while the row is still in
the state the engine read (an optimistic check), so two runs can never both move it. Each write
commits its state change events, their `automations.execution_state_changed.v1` outbox messages
and the next job in the same transaction.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from htco_application.audit import AuditRecord
from htco_application.automation.durable import StateChange, StoredExecution
from htco_application.automation.records import DefinitionRow, ExecutionPage, ExecutionSummary
from htco_application.jobs import JobSpec, JsonValue
from htco_application.status import execution_state_event
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import (
    AutomationDefinition,
    AutomationExecution,
    ExecutionState,
    TriggerKind,
)
from htco_persistence.audit import append_audit
from htco_persistence.database import Database
from htco_persistence.jobs import enqueue_in
from htco_persistence.messaging import add_outbox_in

_EXECUTION_COLUMNS = """
id, automation_id, automation_version, idempotency_key, trigger, actor_kind, actor_id, state,
attempts, correlation_id, organization_id, shop_id, causation_id, last_error, payload,
created_at, updated_at, output, waiting_for
"""


def _execution(row: Sequence[Any]) -> AutomationExecution:
    return AutomationExecution(
        id=row[0],
        automation_id=row[1],
        automation_version=row[2],
        idempotency_key=row[3],
        trigger=TriggerKind(row[4]),
        actor=ActorRef(ActorKind(row[5]), row[6]),
        state=ExecutionState(row[7]),
        attempts=row[8],
        correlation_id=row[9],
        organization_id=row[10],
        shop_id=row[11],
        causation_id=row[12],
        last_error=row[13],
        output=row[17],
        waiting_for=row[18],
    )


def _insert_change(connection: psycopg.Connection[Any], change: StateChange) -> None:
    connection.execute(
        """
        insert into app.automation_execution_events
          (id, organization_id, execution_id, from_state, to_state, actor_kind, actor_id,
           correlation_id, reason, occurred_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            change.id,  # the change's own ID, which also keys its outbox event (Codex, #174)
            change.organization_id,
            change.execution_id,
            None if change.from_state is None else change.from_state.value,
            change.to_state.value,
            change.actor.kind.value,
            change.actor.id,
            change.correlation_id,
            change.reason,
            change.at,
        ],
    )
    # One `automations.execution_state_changed.v1` per recorded change, in its transaction (5.18).
    add_outbox_in(connection, execution_state_event(change), now=change.at)


class PostgresExecutionStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        execution: AutomationExecution,
        payload: Mapping[str, JsonValue],
        *,
        change: StateChange,
        job: JobSpec,
    ) -> AutomationExecution:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                insert into app.automation_executions
                  (id, organization_id, shop_id, automation_id, automation_version,
                   idempotency_key, trigger, actor_kind, actor_id, state, attempts,
                   correlation_id, causation_id, payload)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (automation_id, idempotency_key) do nothing
                returning id
                """,
                [
                    execution.id,
                    execution.organization_id,
                    execution.shop_id,
                    execution.automation_id,
                    execution.automation_version,
                    execution.idempotency_key,
                    execution.trigger.value,
                    execution.actor.kind.value,
                    execution.actor.id,
                    execution.state.value,
                    execution.attempts,
                    execution.correlation_id,
                    execution.causation_id,
                    Jsonb(dict(payload)),
                ],
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    f"""
                    select {_EXECUTION_COLUMNS} from app.automation_executions
                    where automation_id = %s and idempotency_key = %s
                    """,  # noqa: S608 - constant column list
                    [execution.automation_id, execution.idempotency_key],
                ).fetchone()
                if existing is None:  # pragma: no cover - the conflict means it exists
                    raise RuntimeError("the execution disappeared")
                return _execution(existing)
            _insert_change(connection, change)
            enqueue_in(connection, job, now=change.at)
        return execution

    def load(self, execution_id: uuid.UUID) -> StoredExecution | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_EXECUTION_COLUMNS} from app.automation_executions where id = %s",  # noqa: S608 - constant column list
                [execution_id],
            ).fetchone()
        if row is None:
            return None
        return StoredExecution(execution=_execution(row), payload=row[14])

    def advance(
        self,
        before: AutomationExecution,
        steps: Sequence[tuple[AutomationExecution, StateChange]],
        *,
        job: JobSpec | None = None,
        audit: AuditRecord | None = None,
    ) -> bool:
        after, _ = steps[-1]
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                update app.automation_executions
                set state = %s, attempts = %s, last_error = %s, output = %s, waiting_for = %s
                where id = %s and organization_id = %s and state = %s and attempts = %s
                  and waiting_for is not distinct from %s
                returning id
                """,
                [
                    after.state.value,
                    after.attempts,
                    after.last_error,
                    None if after.output is None else Jsonb(dict(after.output)),
                    after.waiting_for,
                    before.id,
                    before.organization_id,
                    before.state.value,
                    before.attempts,
                    before.waiting_for,
                ],
            ).fetchone()
            if row is None:
                return False
            for _, change in steps:
                _insert_change(connection, change)
            if job is not None:
                enqueue_in(connection, job, now=steps[-1][1].at)
            if audit is not None:
                append_audit(connection, audit)
        return True

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "select organization_id from app.shops where id = %s", [shop_id]
            ).fetchone()
        return None if row is None else row[0]


class PostgresAutomationCatalog:
    """The current definition of each automation (its highest version)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def current(self) -> list[DefinitionRow]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select distinct on (id) id, version, document, specification_hash,
                       registry_commit, synced_at
                from app.automation_definitions
                order by id, version desc
                """
            ).fetchall()
        return [
            DefinitionRow(
                document=row[2],
                specification_hash=row[3],
                registry_commit=row[4],
                synced_at=row[5],
            )
            for row in rows
        ]

    def upsert(self, rows: Sequence[tuple[AutomationDefinition, DefinitionRow]]) -> None:
        """Write the sync's changes in one transaction (the sync command only)."""
        with self._database.connection() as connection, connection.transaction():
            for definition, row in rows:
                connection.execute(
                    """
                    insert into app.automation_definitions
                      (id, version, name, business_owner, primary_trigger, callable_from,
                       retry_max_attempts, retry_initial_backoff_seconds,
                       retry_max_backoff_seconds, timeout_seconds, side_effect,
                       approval_required, ai_steps, readiness, activation_phase, user_capability,
                       specification, document, specification_hash, registry_commit, synced_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    on conflict (id, version) do update set
                      name = excluded.name,
                      business_owner = excluded.business_owner,
                      primary_trigger = excluded.primary_trigger,
                      callable_from = excluded.callable_from,
                      retry_max_attempts = excluded.retry_max_attempts,
                      retry_initial_backoff_seconds = excluded.retry_initial_backoff_seconds,
                      retry_max_backoff_seconds = excluded.retry_max_backoff_seconds,
                      timeout_seconds = excluded.timeout_seconds,
                      side_effect = excluded.side_effect,
                      approval_required = excluded.approval_required,
                      ai_steps = excluded.ai_steps,
                      readiness = excluded.readiness,
                      activation_phase = excluded.activation_phase,
                      user_capability = excluded.user_capability,
                      specification = excluded.specification,
                      document = excluded.document,
                      specification_hash = excluded.specification_hash,
                      registry_commit = excluded.registry_commit,
                      synced_at = excluded.synced_at
                    """,
                    [
                        definition.id,
                        definition.version,
                        definition.name,
                        definition.business_owner,
                        definition.primary_trigger.value,
                        sorted(t.value for t in definition.callable_from),
                        definition.retry.max_attempts,
                        int(definition.retry.initial_backoff.total_seconds()),
                        int(definition.retry.max_backoff.total_seconds()),
                        int(definition.timeout.total_seconds()),
                        definition.side_effect.value,
                        definition.approval_required,
                        list(definition.ai_steps),
                        definition.readiness.value,
                        definition.activation_phase,
                        definition.user_capability,
                        Jsonb(dict(row.document.get("specification", {}))),
                        Jsonb(dict(row.document)),
                        row.specification_hash,
                        row.registry_commit,
                        row.synced_at,
                    ],
                )


class PostgresExecutionReader:
    """Executions as the signed-in user sees them (RLS: organization-wide roles, granted shops)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_executions(
        self,
        claims: Mapping[str, Any],
        organization_id: uuid.UUID,
        automation_id: str,
        *,
        before: uuid.UUID | None,
        limit: int,
    ) -> ExecutionPage:
        def run() -> ExecutionPage:
            with self._database.user_transaction(claims) as connection:
                rows = connection.execute(
                    """
                    select id, automation_id, automation_version, shop_id, trigger, actor_kind,
                           actor_id, state, attempts, correlation_id, last_error, created_at,
                           updated_at
                    from app.automation_executions
                    where organization_id = %s and automation_id = %s
                      and (%s::uuid is null or id < %s::uuid)
                    order by id desc
                    limit %s
                    """,
                    [organization_id, automation_id, before, before, limit + 1],
                ).fetchall()
            items = [
                ExecutionSummary(
                    id=row[0],
                    automation_id=row[1],
                    automation_version=row[2],
                    shop_id=row[3],
                    trigger=TriggerKind(row[4]),
                    actor=ActorRef(ActorKind(row[5]), row[6]),
                    state=ExecutionState(row[7]),
                    attempts=row[8],
                    correlation_id=row[9],
                    last_error=row[10],
                    created_at=row[11],
                    updated_at=row[12],
                )
                for row in rows[:limit]
            ]
            more = len(rows) > limit
            return ExecutionPage(items=tuple(items), next_before=items[-1].id if more else None)

        return await anyio.to_thread.run_sync(run)
