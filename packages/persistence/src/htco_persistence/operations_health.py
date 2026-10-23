"""Queue, execution and sync health in Postgres (slice 5.9): the admin screen's reads and the
audited retry of a dead job. IDs, kinds, states, counts, times and error codes only: no job or
outbox payload is read, beyond the execution ID of the engine's own jobs."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from htco_application.audit import AuditRecord
from htco_application.operations_health import (
    DEAD_LETTERS,
    METRICS_WINDOW,
    RECENT_EXECUTIONS,
    DeadJob,
    ExecutionStat,
    FoundJob,
    HealthSnapshot,
    JobCount,
    OutboxFailure,
    RecentExecution,
    ShopSync,
    WebhookCount,
    WorkerBeat,
)
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

_DAY = timedelta(days=1)
_METRICS_ROWS = 5_000

# The organization's jobs and the system's (no organization); finished ones are pruned anyway.
_JOB_COUNTS = """
select kind, state, count(*)::int
from jobs.jobs
where (organization_id = %s or organization_id is null)
group by kind, state
"""

_DEAD = """
select id, kind, shop_id, attempts, last_error, finished_at, payload ->> 'execution_id'
from jobs.jobs
where state = 'dead' and (organization_id = %s or organization_id is null)
order by finished_at desc
limit %s
"""

_WORKERS = """
select worker_id, release, started_at, last_beat_at, current_job_id, stopping
from jobs.worker_heartbeats
where last_beat_at > %s
order by last_beat_at desc
limit 20
"""

_SHOPS = """
select c.shop_id, c.resource, c.kind, c.last_started_at, c.last_succeeded_at, c.last_error,
       (select count(*)::int from app.sync_drifts d
         where d.shop_id = c.shop_id and d.detected_at > %s),
       (select count(*)::int from app.sync_drifts d where d.shop_id = c.shop_id)
from app.sync_cursors c
where c.organization_id = %s
order by c.shop_id, c.resource, c.kind
"""

_WEBHOOKS = """
select source, event_type,
       (count(*) filter (where received_at > %s))::int,
       (count(*) filter (where processed_at is null))::int
from jobs.inbox
where organization_id = %s
group by source, event_type
"""

_EXECUTIONS = """
select id, automation_id, shop_id, state, attempts, last_error, created_at, updated_at
from app.automation_executions
where organization_id = %s
order by created_at desc
limit %s
"""

_OUTBOX = """
select id, destination, status, attempts, last_error, next_attempt_at, action_execution_id
from jobs.outbox
where organization_id = %s and (status = 'dead' or (status = 'pending' and attempts > 0))
order by updated_at desc
limit 50
"""

# Final states have a duration: from the trigger to the execution's last change.
_STATS = """
select automation_id, state, attempts,
       case when state in ('succeeded', 'dead_lettered', 'failed', 'cancelled', 'compensated')
            then extract(epoch from updated_at - created_at)::float8 end
from app.automation_executions
where organization_id = %s and created_at > %s
order by created_at desc
limit %s
"""

_FIND = """
select id, kind, state, attempts, last_error, payload ->> 'execution_id'
from jobs.jobs
where id = %s and organization_id = %s
"""

_RETRY = """
update jobs.jobs
set state = 'queued', attempts = 0, run_at = %s, finished_at = null
where id = %s and organization_id = %s and state = 'dead'
"""


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


class PostgresOperationsHealth:
    """`HealthReader` and `DeadJobs` on the API's connection."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def snapshot(self, organization_id: uuid.UUID, *, now: datetime) -> HealthSnapshot:
        day = now - _DAY
        with self._database.connection() as c:
            jobs = c.execute(_JOB_COUNTS, [organization_id]).fetchall()
            dead = c.execute(_DEAD, [organization_id, DEAD_LETTERS]).fetchall()
            workers = c.execute(_WORKERS, [now - timedelta(days=7)]).fetchall()
            shops = c.execute(_SHOPS, [day, organization_id]).fetchall()
            hooks = c.execute(_WEBHOOKS, [day, organization_id]).fetchall()
            runs = c.execute(_EXECUTIONS, [organization_id, RECENT_EXECUTIONS]).fetchall()
            outbox = c.execute(_OUTBOX, [organization_id]).fetchall()
            stats = c.execute(
                _STATS, [organization_id, now - METRICS_WINDOW, _METRICS_ROWS]
            ).fetchall()
        return HealthSnapshot(
            jobs=tuple(JobCount(r[0], r[1], r[2]) for r in jobs),
            dead_letters=tuple(
                DeadJob(r[0], r[1], r[2], r[3], r[4], r[5], _uuid(r[6])) for r in dead
            ),
            workers=tuple(WorkerBeat(r[0], r[1], r[2], r[3], r[4], r[5]) for r in workers),
            shops=tuple(ShopSync(*r) for r in shops),
            webhooks=tuple(WebhookCount(r[0], r[1], r[2], r[3]) for r in hooks),
            executions=tuple(RecentExecution(*r) for r in runs),
            outbox_failures=tuple(OutboxFailure(*r) for r in outbox),
            execution_stats=tuple(ExecutionStat(r[0], r[1], r[2], r[3]) for r in stats),
        )

    def find(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> FoundJob | None:
        with self._database.connection() as c:
            row = c.execute(_FIND, [job_id, organization_id]).fetchone()
        if row is None:
            return None
        return FoundJob(row[0], row[1], row[2], row[3], row[4], _uuid(row[5]))

    def retry(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, now: datetime, audit: AuditRecord
    ) -> bool:
        with self._database.connection() as c, c.transaction():
            moved = c.execute(_RETRY, [now, job_id, organization_id]).rowcount
            if moved != 1:
                return False
            append_audit(c, audit)
        return True
