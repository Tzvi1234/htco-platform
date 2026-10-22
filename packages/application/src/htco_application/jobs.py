"""The `JobQueue` port (ADR-0007, slice 5.1): durable, at-least-once background work.

Use cases enqueue a `JobSpec`; the worker claims jobs with a lease, extends the lease while a job
runs (the heartbeat), completes it, or fails it into a retry or the dead-letter state. Jobs are
unique by `(kind, idempotency_key)`: enqueueing the same job twice returns the first one. A job
with a `lock_key` never runs while another job with the same key runs (one sync per shop at a
time). Jobs that belong to a tenant carry its `organization_id` (and `shop_id`), and the worker
runs them inside that tenant's database scope.

The port is synchronous: its only adapter is Postgres (psycopg's pool), and the worker calls it
from a thread, like every other repository.
"""

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from htco_domain.automation import RetryPolicy
from htco_domain.jobs import MAX_KEY_LENGTH, FailurePlan, Schedule, check_job_kind

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

# Three attempts, 10 s then 20 s apart (with jitter): enough for a passing outage.
DEFAULT_RETRY = RetryPolicy(
    max_attempts=3, initial_backoff=timedelta(seconds=10), max_backoff=timedelta(minutes=5)
)
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class JobSpec:
    kind: str
    idempotency_key: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    organization_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    lock_key: str | None = None
    run_at: datetime | None = None  # None: as soon as possible
    retry: RetryPolicy = DEFAULT_RETRY
    priority: int = 0
    # What started the work, and the step that caused this job (ADR-0019): carried from the
    # trigger to the job, the action and the outbox, so logs and records join up.
    correlation_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self) -> None:
        check_job_kind(self.kind)
        for name in ("correlation_id", "causation_id"):
            value = getattr(self, name)
            if value is not None and not 0 < len(value) <= MAX_KEY_LENGTH:
                raise ValueError(f"a {name} is 1 to {MAX_KEY_LENGTH} characters")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > MAX_KEY_LENGTH:
            raise ValueError(f"a job key is 1 to {MAX_KEY_LENGTH} characters")
        if self.lock_key is not None and not 0 < len(self.lock_key) <= MAX_KEY_LENGTH:
            raise ValueError(f"a lock key is 1 to {MAX_KEY_LENGTH} characters")
        if self.shop_id is not None and self.organization_id is None:
            raise ValueError("a shop's job also names the shop's organization")


@dataclass(frozen=True)
class ClaimedJob:
    """A job a worker holds until `lease_expires_at` (extended by its heartbeat)."""

    id: uuid.UUID
    kind: str
    idempotency_key: str
    payload: Mapping[str, JsonValue]
    organization_id: uuid.UUID | None
    shop_id: uuid.UUID | None
    lock_key: str | None
    attempts: int  # including this one
    retry: RetryPolicy
    lease_expires_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass(frozen=True)
class DeadLetter:
    """A job whose retries are exhausted, for the admins (AC-5.1.3). Never its payload."""

    id: uuid.UUID
    kind: str
    organization_id: uuid.UUID | None
    shop_id: uuid.UUID | None
    attempts: int
    last_error: str | None
    created_at: datetime
    failed_at: datetime


class JobQueue(Protocol):
    def enqueue(self, spec: JobSpec, *, now: datetime) -> uuid.UUID:
        """Add the job unless `(kind, idempotency_key)` exists; return the job's ID either way."""
        ...

    def claim(
        self, *, worker_id: str, kinds: Collection[str], now: datetime, lease: timedelta
    ) -> ClaimedJob | None:
        """Atomically take the next due job of one of `kinds` whose lock is free, or None."""
        ...

    def extend_lease(
        self, job_id: uuid.UUID, *, worker_id: str, now: datetime, lease: timedelta
    ) -> bool:
        """False when the worker no longer holds the job (its lease expired and was released)."""
        ...

    def complete(self, job_id: uuid.UUID, *, worker_id: str, now: datetime) -> bool:
        """Mark the job succeeded; False (nothing changed) when the worker lost the lease."""
        ...

    def fail(
        self, job_id: uuid.UUID, *, worker_id: str, error: str, plan: FailurePlan, now: datetime
    ) -> bool:
        """Record the failed attempt and follow `plan` (a retry time, or dead)."""
        ...

    def release_expired(self, *, now: datetime) -> int:
        """Running jobs whose lease expired (their worker crashed) become due again, or dead when
        their attempts are used up. Returns how many were released."""
        ...

    def dead_letters(self, *, organization_id: uuid.UUID | None, limit: int) -> list[DeadLetter]:
        """Dead jobs, newest first: one organization's, or every one when `organization_id` is
        None (operators only)."""
        ...

    def prune_finished(self, *, before: datetime) -> int:
        """Delete succeeded jobs that finished before `before`; dead jobs stay."""
        ...


class WorkerHeartbeats(Protocol):
    def beat(  # noqa: PLR0913 - one argument per column of the heartbeat row
        self,
        *,
        worker_id: str,
        started_at: datetime,
        now: datetime,
        current_job_id: uuid.UUID | None,
        release: str | None,
        stopping: bool,
    ) -> None: ...


class JobError(Exception):
    """A job failure whose message is safe to store and show to admins: no buyer data, no
    secrets. Any other exception is stored by its type name only."""


def describe_error(error: BaseException) -> str:
    """The `last_error` kept with a failed job."""
    name = type(error).__name__
    if isinstance(error, JobError) and str(error):
        return f"{name}: {error}"[:MAX_ERROR_LENGTH]
    return name[:MAX_ERROR_LENGTH]


def due_schedule_jobs(schedules: Collection[Schedule], now: datetime) -> list[JobSpec]:
    """The current period's job of each schedule; enqueueing them again is a no-op."""
    return [
        JobSpec(
            kind=schedule.kind,
            idempotency_key=schedule.key_for(now),
            run_at=schedule.period_start(now),
        )
        for schedule in schedules
    ]
