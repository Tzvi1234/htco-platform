"""Tasks on Postgres (slice 7.1): `app.tasks` and its append-only `app.task_events`.

The store writes a task, its step and its events in one transaction; a repeat of an idempotency
key returns the first task. The reader is the API's (the organization filter in every query; the
use case filters shops by role).
"""

import uuid
from collections.abc import Sequence
from typing import Any

import psycopg

from htco_application.kernel import OutboxMessage
from htco_application.tasks import StaleTask, StoredTask, TaskDetail, TaskStep
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.tasks import BusinessTask, EvidenceKind, TaskEvidence, TaskKind, TaskState
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_COLUMNS = """id, organization_id, shop_id, kind, title, assignee_kind, assignee_id, owner_kind,
owner_id, state, idempotency_key, correlation_id, due_at, evidence_kind, evidence_reference,
created_at, updated_at"""


def _stored(row: Sequence[Any]) -> StoredTask:
    task = BusinessTask(
        id=row[0], organization_id=row[1], shop_id=row[2], kind=TaskKind(row[3]), title=row[4],
        assignee=ActorRef(ActorKind(row[5]), row[6]),
        owner=None if row[7] is None else ActorRef(ActorKind(row[7]), row[8]),
        state=TaskState(row[9]), idempotency_key=row[10], correlation_id=row[11], due_at=row[12],
        evidence=None if row[13] is None else TaskEvidence(EvidenceKind(row[13]), row[14]),
    )  # fmt: skip
    return StoredTask(task, created_at=row[15], updated_at=row[16])


def _insert_step(connection: psycopg.Connection[Any], step: TaskStep) -> None:
    evidence = step.evidence
    connection.execute(
        """
        insert into app.task_events
          (id, organization_id, task_id, from_state, to_state, actor_kind, actor_id, occurred_at,
           evidence_kind, evidence_reference, correlation_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            uuid.uuid7(), step.organization_id, step.task_id,
            None if step.from_state is None else step.from_state.value, step.to_state.value,
            step.actor.kind.value, step.actor.id, step.at,
            None if evidence is None else evidence.kind.value,
            None if evidence is None else evidence.reference,
            None if step.correlation_id is None else step.correlation_id[:200],
        ],
    )  # fmt: skip


class PostgresTaskStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self, stored: StoredTask, step: TaskStep, events: tuple[OutboxMessage, ...]
    ) -> tuple[StoredTask, bool]:
        t = stored.task
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                f"""
                insert into app.tasks
                  (id, organization_id, shop_id, kind, title, assignee_kind, assignee_id,
                   owner_kind, owner_id, state, idempotency_key, correlation_id, due_at,
                   created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (organization_id, idempotency_key) do nothing
                returning {_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [
                    t.id, t.organization_id, t.shop_id, t.kind.value, t.title,
                    t.assignee.kind.value, t.assignee.id,
                    None if t.owner is None else t.owner.kind.value,
                    None if t.owner is None else t.owner.id,
                    t.state.value, t.idempotency_key, t.correlation_id, t.due_at,
                    stored.created_at, stored.updated_at,
                ],
            ).fetchone()  # fmt: skip
            if row is not None:
                _insert_step(connection, step)
                for event in events:
                    add_outbox_in(connection, event, now=step.at)
                return _stored(row), True
            existing = connection.execute(
                f"""
                select {_COLUMNS} from app.tasks
                where organization_id = %s and idempotency_key = %s
                """,  # noqa: S608 - a constant column list
                [t.organization_id, t.idempotency_key],
            ).fetchone()
        if existing is None:  # pragma: no cover - deleted between the two statements
            raise StaleTask("the task disappeared")
        return _stored(existing), False

    def get(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> StoredTask | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_COLUMNS} from app.tasks where organization_id = %s and id = %s",  # noqa: S608 - a constant column list
                [organization_id, task_id],
            ).fetchone()
        return None if row is None else _stored(row)

    def move(
        self,
        before: StoredTask,
        after: StoredTask,
        step: TaskStep,
        events: tuple[OutboxMessage, ...],
    ) -> StoredTask:
        task = after.task
        evidence = task.evidence
        with self._database.connection() as connection, connection.transaction():
            locked = connection.execute(
                "select state from app.tasks where id = %s and organization_id = %s for update",
                [task.id, task.organization_id],
            ).fetchone()
            if locked is None or locked[0] != before.task.state.value:
                raise StaleTask("the task moved meanwhile")
            _insert_step(connection, step)
            row = connection.execute(
                f"""
                update app.tasks
                set state = %s, evidence_kind = %s, evidence_reference = %s, updated_at = %s
                where id = %s and organization_id = %s
                returning {_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [
                    task.state.value,
                    None if evidence is None else evidence.kind.value,
                    None if evidence is None else evidence.reference,
                    after.updated_at, task.id, task.organization_id,
                ],
            ).fetchone()  # fmt: skip
            for event in events:
                add_outbox_in(connection, event, now=step.at)
        if row is None:  # pragma: no cover - locked above
            raise StaleTask("the task disappeared")
        return _stored(row)


class PostgresTaskReader:
    """The task views' reads (the API's pool, the organization filter in every query)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def list_tasks(  # noqa: PLR0913 - the scope, the filters and the page
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        shop_ids: frozenset[uuid.UUID] | None = None,
        states: frozenset[TaskState] | None = None,
        kind: TaskKind | None = None,
        before: uuid.UUID | None = None,
    ) -> tuple[StoredTask, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                select {_COLUMNS} from app.tasks
                where organization_id = %(org)s
                  and (%(shops)s::uuid[] is null or shop_id = any(%(shops)s::uuid[]))
                  and (%(states)s::text[] is null or state = any(%(states)s::text[]))
                  and (%(kind)s::text is null or kind = %(kind)s::text)
                  and (%(before)s::uuid is null or (created_at, id) < (
                    select b.created_at, b.id from app.tasks b
                    where b.organization_id = %(org)s and b.id = %(before)s::uuid))
                order by created_at desc, id desc
                limit %(limit)s
                """,  # noqa: S608 - a constant column list
                {
                    "org": organization_id,
                    "shops": None if shop_ids is None else list(shop_ids),
                    "states": None if states is None else sorted(s.value for s in states),
                    "kind": None if kind is None else kind.value,
                    "before": before,
                    "limit": limit,
                },
            ).fetchall()
        return tuple(_stored(r) for r in rows)

    def task_detail(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> TaskDetail | None:
        with self._database.connection() as connection:
            row = connection.execute(
                f"select {_COLUMNS} from app.tasks where organization_id = %s and id = %s",  # noqa: S608 - a constant column list
                [organization_id, task_id],
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                """
                select from_state, to_state, actor_kind, actor_id, occurred_at, evidence_kind,
                       evidence_reference, correlation_id
                from app.task_events where organization_id = %s and task_id = %s
                order by occurred_at, created_at
                """,
                [organization_id, task_id],
            ).fetchall()
        return TaskDetail(
            stored=_stored(row),
            history=tuple(
                TaskStep(
                    task_id=task_id, organization_id=organization_id,
                    from_state=None if e[0] is None else TaskState(e[0]), to_state=TaskState(e[1]),
                    actor=ActorRef(ActorKind(e[2]), e[3]), at=e[4],
                    evidence=None if e[5] is None else TaskEvidence(EvidenceKind(e[5]), e[6]),
                    correlation_id=e[7],
                )
                for e in events
            ),
        )  # fmt: skip
