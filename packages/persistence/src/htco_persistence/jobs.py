"""The job queue on Postgres (slice 5.1, ADR-0007): `jobs.jobs` claimed with `SKIP LOCKED`.

Every method is one short transaction. Times come from the caller's clock, so tests can move
time; the workers pass their real clock. A claim takes the oldest due job of the worker's kinds
whose `lock_key` no running job holds; if two workers claim two jobs with the same lock key at
the same instant, the partial unique index on running lock keys refuses the second, and that
worker simply finds no job this time (AC-5.1.2). Completing or failing a job checks that the
worker still holds its lease, so a worker that lost its job (it ran past its lease and the job was
released) cannot overwrite the new attempt.
"""

import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from htco_application.jobs import ClaimedJob, DeadLetter, JobSpec
from htco_domain.automation import RetryPolicy
from htco_domain.jobs import FailurePlan, JobState
from htco_persistence.database import Database

_CLAIMED = """
id, kind, idempotency_key, payload, organization_id, shop_id, lock_key, attempts, max_attempts,
initial_backoff_ms, max_backoff_ms, lease_expires_at, correlation_id, causation_id
"""
# The claim's update joins its candidate (which has an `id` too): name the job's columns.
_CLAIMED_BY_ALIAS = ", ".join(f"j.{column.strip()}" for column in _CLAIMED.split(","))


def _ms(delta: timedelta) -> int:
    return int(delta.total_seconds() * 1000)


def enqueue_in(connection: psycopg.Connection[Any], spec: JobSpec, *, now: datetime) -> uuid.UUID:
    """Enqueue inside the caller's transaction (an inbox insert or a business change and its job
    commit together). Returns the existing job's ID when the key is taken."""
    row = connection.execute(
        """
        insert into jobs.jobs
          (id, kind, idempotency_key, organization_id, shop_id, lock_key, payload, priority,
           max_attempts, initial_backoff_ms, max_backoff_ms, run_at, correlation_id, causation_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (kind, idempotency_key) do nothing
        returning id
        """,
        [
            uuid.uuid7(),
            spec.kind,
            spec.idempotency_key,
            spec.organization_id,
            spec.shop_id,
            spec.lock_key,
            Jsonb(dict(spec.payload)),
            spec.priority,
            spec.retry.max_attempts,
            _ms(spec.retry.initial_backoff),
            _ms(spec.retry.max_backoff),
            spec.run_at or now,
            spec.correlation_id,
            spec.causation_id,
        ],
    ).fetchone()
    if row is None:
        row = connection.execute(
            "select id from jobs.jobs where kind = %s and idempotency_key = %s",
            [spec.kind, spec.idempotency_key],
        ).fetchone()
        if row is None:  # pragma: no cover - pruned between the two statements
            raise RuntimeError("the job disappeared while it was enqueued")
    job_id: uuid.UUID = row[0]
    return job_id


def _claimed(row: tuple[Any, ...]) -> ClaimedJob:
    return ClaimedJob(
        id=row[0],
        kind=row[1],
        idempotency_key=row[2],
        payload=row[3],
        organization_id=row[4],
        shop_id=row[5],
        lock_key=row[6],
        attempts=row[7],
        retry=RetryPolicy(
            max_attempts=row[8],
            initial_backoff=timedelta(milliseconds=row[9]),
            max_backoff=timedelta(milliseconds=row[10]),
        ),
        lease_expires_at=row[11],
        correlation_id=row[12],
        causation_id=row[13],
    )


class PostgresJobQueue:
    def __init__(self, database: Database) -> None:
        self._database = database

    def enqueue(self, spec: JobSpec, *, now: datetime) -> uuid.UUID:
        with self._database.connection() as connection, connection.transaction():
            return enqueue_in(connection, spec, now=now)

    def claim(
        self, *, worker_id: str, kinds: Collection[str], now: datetime, lease: timedelta
    ) -> ClaimedJob | None:
        try:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    f"""
                    with candidate as (
                      select j.id from jobs.jobs j
                      where j.state in ('queued', 'retry_scheduled')
                        and j.run_at <= %(now)s
                        and j.kind = any(%(kinds)s)
                        and (j.lock_key is null or not exists (
                          select 1 from jobs.jobs held
                          where held.lock_key = j.lock_key and held.state = 'running'))
                      order by j.priority desc, j.run_at, j.id
                      limit 1
                      for update skip locked
                    )
                    update jobs.jobs j
                    set state = 'running', attempts = j.attempts + 1, leased_by = %(worker)s,
                        lease_expires_at = %(lease_until)s
                    from candidate
                    where j.id = candidate.id
                    returning {_CLAIMED_BY_ALIAS}
                    """,  # noqa: S608 - constant column list
                    {
                        "now": now,
                        "kinds": list(kinds),
                        "worker": worker_id,
                        "lease_until": now + lease,
                    },
                ).fetchone()
        except errors.UniqueViolation:
            # Another worker started a job with the same lock key at the same moment.
            return None
        return None if row is None else _claimed(row)

    def extend_lease(
        self, job_id: uuid.UUID, *, worker_id: str, now: datetime, lease: timedelta
    ) -> bool:
        return self._update_held(
            "update jobs.jobs set lease_expires_at = %s "
            "where id = %s and state = 'running' and leased_by = %s returning id",
            [now + lease, job_id, worker_id],
        )

    def complete(self, job_id: uuid.UUID, *, worker_id: str, now: datetime) -> bool:
        return self._update_held(
            """
            update jobs.jobs
            set state = 'succeeded', leased_by = null, lease_expires_at = null, finished_at = %s,
                last_error = null
            where id = %s and state = 'running' and leased_by = %s
            returning id
            """,
            [now, job_id, worker_id],
        )

    def fail(
        self, job_id: uuid.UUID, *, worker_id: str, error: str, plan: FailurePlan, now: datetime
    ) -> bool:
        dead = plan.state is JobState.DEAD
        return self._update_held(
            """
            update jobs.jobs
            set state = %s, leased_by = null, lease_expires_at = null, last_error = %s,
                run_at = coalesce(%s, run_at), finished_at = %s
            where id = %s and state = 'running' and leased_by = %s
            returning id
            """,
            [
                plan.state.value,
                error,
                plan.retry_at,
                now if dead else None,
                job_id,
                worker_id,
            ],
        )

    def release_expired(self, *, now: datetime) -> int:
        """A crash counts as an attempt, so a job that kills its worker every time still ends in
        the dead-letter state instead of looping."""
        with self._database.connection() as connection, connection.transaction():
            released = connection.execute(
                """
                update jobs.jobs
                set state = case when attempts >= max_attempts then 'dead' else 'retry_scheduled'
                            end,
                    finished_at = case when attempts >= max_attempts then %(now)s end,
                    run_at = %(now)s,
                    leased_by = null, lease_expires_at = null,
                    last_error = 'LeaseExpired: the worker stopped before finishing the job'
                where state = 'running' and lease_expires_at < %(now)s
                returning id
                """,
                {"now": now},
            ).fetchall()
        return len(released)

    def dead_letters(self, *, organization_id: uuid.UUID | None, limit: int) -> list[DeadLetter]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                select id, kind, organization_id, shop_id, attempts, last_error, created_at,
                       finished_at
                from jobs.jobs
                where state = 'dead'
                  and (%(organization)s::uuid is null or organization_id = %(organization)s::uuid)
                order by finished_at desc, id desc
                limit %(limit)s
                """,
                {"organization": organization_id, "limit": limit},
            ).fetchall()
        return [DeadLetter(*row) for row in rows]

    def prune_finished(self, *, before: datetime) -> int:
        """Delete succeeded jobs that finished before `before` (dead ones stay for the admins)."""
        with self._database.connection() as connection, connection.transaction():
            deleted = connection.execute(
                "delete from jobs.jobs where state = 'succeeded' and finished_at < %s returning id",
                [before],
            ).fetchall()
        return len(deleted)

    def _update_held(self, statement: str, params: list[object]) -> bool:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(statement, params).fetchone()
        return row is not None


class PostgresWorkerHeartbeats:
    def __init__(self, database: Database) -> None:
        self._database = database

    def beat(  # noqa: PLR0913 - one argument per column of the heartbeat row
        self,
        *,
        worker_id: str,
        started_at: datetime,
        now: datetime,
        current_job_id: uuid.UUID | None,
        release: str | None,
        stopping: bool,
    ) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                insert into jobs.worker_heartbeats
                  (worker_id, release, started_at, last_beat_at, current_job_id, stopping)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (worker_id) do update set
                  release = excluded.release,
                  started_at = excluded.started_at,
                  last_beat_at = excluded.last_beat_at,
                  current_job_id = excluded.current_job_id,
                  stopping = excluded.stopping
                """,
                [worker_id, release, started_at, now, current_job_id, stopping],
            )
