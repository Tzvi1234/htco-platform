"""Queue, execution and sync health for the Owner and Admins (slice 5.9).

One read, `operations_health`, answers the admin screen: jobs by kind and state, dead letters
with their last error, worker heartbeats, the last reconciliation of each shop with its drifts,
webhook intake counts, the newest automation executions (each links to its chain view,
`GET /v1/executions/{id}`), outbox delivery failures, and per automation its executions,
success rate, retries, dead letters and p50/p95 latency over the last seven days.

One write, `retry_dead_job`, sends a dead-lettered job back to the queue, audited in the same
transaction. An execution's job is retried through its execution instead (the engine's own
audited retry), so the execution's state machine stays the only truth.

Everything is IDs, kinds, states, counts, times and error codes: no payload and no buyer data.
"""

import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide
from htco_application.audit import AuditRecord
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef

# The engine's job kind; its retries go through the execution (`retry_as_admin`).
EXECUTION_JOB_KIND = "automation.execute"
# A worker whose last beat is older than this is shown as stopped (two leases of 60 s).
WORKER_STALE_AFTER = timedelta(minutes=2)
# Reconciliation runs twice a day (slice 5.5): a shop with no success for longer is stale.
SYNC_STALE_AFTER = timedelta(hours=13)
# The metrics' window, and how many executions they read at most.
METRICS_WINDOW = timedelta(days=7)
RECENT_EXECUTIONS = 25
DEAD_LETTERS = 50


# --- What the screen shows ------------------------------------------------------------------


@dataclass(frozen=True)
class JobCount:
    kind: str
    state: str  # queued, running, retry_scheduled, succeeded, dead
    count: int


@dataclass(frozen=True)
class DeadJob:
    id: uuid.UUID
    kind: str
    shop_id: uuid.UUID | None
    attempts: int
    # An exception type, or a message the code marked safe (the queue's own rule).
    last_error: str | None
    finished_at: datetime
    # For the engine's jobs: the execution to retry and to open in the chain view.
    execution_id: uuid.UUID | None


@dataclass(frozen=True)
class WorkerBeat:
    worker_id: str
    release: str | None
    started_at: datetime
    last_beat_at: datetime
    current_job_id: uuid.UUID | None
    stopping: bool


@dataclass(frozen=True)
class WorkerView:
    beat: WorkerBeat
    alive: bool
    age_seconds: int


@dataclass(frozen=True)
class ShopSync:
    shop_id: uuid.UUID
    resource: str  # orders
    kind: str  # reconcile, sweep, backfill…
    last_started_at: datetime | None
    last_succeeded_at: datetime | None
    last_error: str | None
    drifts_last_day: int
    drifts_total: int


@dataclass(frozen=True)
class ShopSyncView:
    sync: ShopSync
    stale: bool


@dataclass(frozen=True)
class WebhookCount:
    source: str
    event_type: str
    received_last_day: int
    unprocessed: int


@dataclass(frozen=True)
class RecentExecution:
    id: uuid.UUID
    automation_id: str
    shop_id: uuid.UUID | None
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OutboxFailure:
    id: uuid.UUID
    destination: str
    status: str  # pending (retrying) or dead
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    action_execution_id: uuid.UUID | None


@dataclass(frozen=True)
class ExecutionStat:
    """One execution of the metrics' window: its state, attempts and, once final, how long it
    took from trigger to its last change."""

    automation_id: str
    state: str
    attempts: int
    duration_seconds: float | None


@dataclass(frozen=True)
class AutomationMetrics:
    automation_id: str
    executions: int
    succeeded: int
    dead_lettered: int
    retries: int  # attempts beyond the first
    p50_seconds: float | None
    p95_seconds: float | None

    @property
    def success_rate(self) -> Decimal | None:
        """Succeeded among the finished ones (succeeded or dead-lettered), to one decimal."""
        finished = self.succeeded + self.dead_lettered
        if finished == 0:
            return None
        return (Decimal(self.succeeded) * 100 / Decimal(finished)).quantize(Decimal("0.1"))


@dataclass(frozen=True)
class HealthSnapshot:
    jobs: tuple[JobCount, ...]
    dead_letters: tuple[DeadJob, ...]
    workers: tuple[WorkerBeat, ...]
    shops: tuple[ShopSync, ...]
    webhooks: tuple[WebhookCount, ...]
    executions: tuple[RecentExecution, ...]
    outbox_failures: tuple[OutboxFailure, ...]
    execution_stats: tuple[ExecutionStat, ...]


@dataclass(frozen=True)
class OperationsHealth:
    organization_id: uuid.UUID
    generated_at: datetime
    jobs: tuple[JobCount, ...]
    dead_letters: tuple[DeadJob, ...]
    workers: tuple[WorkerView, ...]
    shops: tuple[ShopSyncView, ...]
    webhooks: tuple[WebhookCount, ...]
    executions: tuple[RecentExecution, ...]
    outbox_failures: tuple[OutboxFailure, ...]
    automations: tuple[AutomationMetrics, ...]


# --- Ports ----------------------------------------------------------------------------------


class HealthReader(Protocol):
    def snapshot(self, organization_id: uuid.UUID, *, now: datetime) -> HealthSnapshot:
        """The organization's jobs (and the system's, without an organization), heartbeats,
        syncs, webhooks, executions and outbox failures; the execution stats since
        `now - METRICS_WINDOW`."""
        ...


@dataclass(frozen=True)
class FoundJob:
    id: uuid.UUID
    kind: str
    state: str
    attempts: int
    last_error: str | None
    execution_id: uuid.UUID | None


class DeadJobs(Protocol):
    def find(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> FoundJob | None:
        """The organization's job in any state (None: no such job of this organization)."""
        ...

    def retry(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, now: datetime, audit: AuditRecord
    ) -> bool:
        """Queue the dead job again (attempts from zero, due now) and write `audit`, in one
        transaction; False when it is no longer dead."""
        ...


# --- Errors ---------------------------------------------------------------------------------


class OperationsForbiddenError(Exception):
    """Only the Owner and Admins see and retry the queue."""


class DeadJobNotFoundError(Exception):
    pass


class JobNotDeadError(Exception):
    pass


class RetryThroughExecutionError(Exception):
    """The engine's job is retried through its execution, never on its own."""

    def __init__(self, execution_id: uuid.UUID | None) -> None:
        super().__init__("retry the execution instead")
        self.execution_id = execution_id


# --- Use cases ------------------------------------------------------------------------------


def _organization_admin(
    auth: AuthContext, organization_id: uuid.UUID | None, now: datetime
) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)
    decision = decide(principal, Capability.RETRY_SYNC, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW or decision.scope is not Scope.ORGANIZATION:
        raise OperationsForbiddenError(decision.outcome)
    return principal.organization_id


def operations_health(
    reader: HealthReader,
    auth: AuthContext,
    *,
    organization_id: uuid.UUID | None = None,
    now: datetime,
) -> OperationsHealth:
    organization = _organization_admin(auth, organization_id, now)
    found = reader.snapshot(organization, now=now)
    return OperationsHealth(
        organization_id=organization,
        generated_at=now,
        jobs=tuple(sorted(found.jobs, key=lambda c: (c.kind, c.state))),
        dead_letters=tuple(
            sorted(found.dead_letters, key=lambda d: d.finished_at, reverse=True)[:DEAD_LETTERS]
        ),
        workers=tuple(
            WorkerView(
                beat=beat,
                alive=not beat.stopping and now - beat.last_beat_at <= WORKER_STALE_AFTER,
                age_seconds=max(0, int((now - beat.last_beat_at).total_seconds())),
            )
            for beat in sorted(found.workers, key=lambda b: b.last_beat_at, reverse=True)
        ),
        shops=tuple(
            ShopSyncView(
                sync=sync,
                stale=sync.last_succeeded_at is None
                or now - sync.last_succeeded_at > SYNC_STALE_AFTER,
            )
            for sync in found.shops
        ),
        webhooks=tuple(sorted(found.webhooks, key=lambda w: (w.source, w.event_type))),
        executions=tuple(
            sorted(found.executions, key=lambda e: e.created_at, reverse=True)[:RECENT_EXECUTIONS]
        ),
        outbox_failures=found.outbox_failures,
        automations=automation_metrics(found.execution_stats),
    )


def _percentile(sorted_values: Sequence[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Inclusive linear interpolation, like Postgres' percentile_cont.
    cuts = statistics.quantiles(sorted_values, n=100, method="inclusive")
    return cuts[round(fraction * 100) - 1]


def automation_metrics(stats: Iterable[ExecutionStat]) -> tuple[AutomationMetrics, ...]:
    """Per automation: executions, succeeded, dead-lettered, retries and p50/p95 latency of the
    finished ones."""
    grouped: dict[str, list[ExecutionStat]] = defaultdict(list)
    for stat in stats:
        grouped[stat.automation_id].append(stat)
    found = []
    for automation_id, rows in sorted(grouped.items()):
        durations = sorted(r.duration_seconds for r in rows if r.duration_seconds is not None)
        found.append(
            AutomationMetrics(
                automation_id=automation_id,
                executions=len(rows),
                succeeded=sum(1 for r in rows if r.state == "succeeded"),
                dead_lettered=sum(1 for r in rows if r.state == "dead_lettered"),
                retries=sum(max(0, r.attempts - 1) for r in rows),
                p50_seconds=_percentile(durations, 0.5),
                p95_seconds=_percentile(durations, 0.95),
            )
        )
    return tuple(found)


def retry_dead_job(  # noqa: PLR0913 - the store, the caller, the job and the request
    jobs: DeadJobs,
    auth: AuthContext,
    job_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None = None,
    correlation_id: str,
    now: datetime,
) -> None:
    """An Owner or Admin sends a dead job back to the queue; audited with the job's kind and
    last error (never its payload)."""
    organization = _organization_admin(auth, organization_id, now)
    found = jobs.find(organization, job_id)
    if found is None:
        raise DeadJobNotFoundError(job_id)
    if found.kind == EXECUTION_JOB_KIND:
        raise RetryThroughExecutionError(found.execution_id)
    if found.state != "dead":
        raise JobNotDeadError(job_id)
    audit = AuditRecord.new(
        organization_id=organization,
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        action="job.dead_letter_retried",
        entity_type="job",
        entity_id=str(job_id),
        before={"state": "dead", "attempts": found.attempts, "last_error": found.last_error},
        after={"state": "queued", "kind": found.kind},
        correlation_id=correlation_id,
    )
    if not jobs.retry(organization, job_id, now=now, audit=audit):
        raise JobNotDeadError(job_id)
