"""An in-memory job queue with the Postgres queue's rules (slice 5.1), for worker and use-case
tests. The integration tests prove the same rules against real Postgres."""

import threading
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from htco_application.jobs import ClaimedJob, DeadLetter, JobSpec
from htco_domain.jobs import FailurePlan, JobState


@dataclass
class StoredJob:
    id: uuid.UUID
    spec: JobSpec
    state: JobState
    run_at: datetime
    created_at: datetime
    attempts: int = 0
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    finished_at: datetime | None = None


@dataclass
class MemoryJobQueue:
    jobs: dict[uuid.UUID, StoredJob] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def enqueue(self, spec: JobSpec, *, now: datetime) -> uuid.UUID:
        with self._guard:
            for job in self.jobs.values():
                if (job.spec.kind, job.spec.idempotency_key) == (spec.kind, spec.idempotency_key):
                    return job.id
            job_id = uuid.uuid7()
            self.jobs[job_id] = StoredJob(
                id=job_id,
                spec=spec,
                state=JobState.QUEUED,
                run_at=spec.run_at or now,
                created_at=now,
            )
            return job_id

    def claim(
        self, *, worker_id: str, kinds: Collection[str], now: datetime, lease: timedelta
    ) -> ClaimedJob | None:
        with self._guard:
            held = {
                j.spec.lock_key
                for j in self.jobs.values()
                if j.state is JobState.RUNNING and j.spec.lock_key
            }
            due = sorted(
                (
                    j
                    for j in self.jobs.values()
                    if j.state in {JobState.QUEUED, JobState.RETRY_SCHEDULED}
                    and j.run_at <= now
                    and j.spec.kind in kinds
                    and j.spec.lock_key not in held
                ),
                key=lambda j: (-j.spec.priority, j.run_at, j.id),
            )
            if not due:
                return None
            job = due[0]
            job.state = JobState.RUNNING
            job.attempts += 1
            job.leased_by = worker_id
            job.lease_expires_at = now + lease
            return self._claimed(job)

    def extend_lease(
        self, job_id: uuid.UUID, *, worker_id: str, now: datetime, lease: timedelta
    ) -> bool:
        with self._guard:
            job = self._held(job_id, worker_id)
            if job is None:
                return False
            job.lease_expires_at = now + lease
            return True

    def complete(self, job_id: uuid.UUID, *, worker_id: str, now: datetime) -> bool:
        with self._guard:
            job = self._held(job_id, worker_id)
            if job is None:
                return False
            job.state, job.finished_at, job.last_error = JobState.SUCCEEDED, now, None
            job.leased_by = job.lease_expires_at = None
            return True

    def fail(
        self, job_id: uuid.UUID, *, worker_id: str, error: str, plan: FailurePlan, now: datetime
    ) -> bool:
        with self._guard:
            job = self._held(job_id, worker_id)
            if job is None:
                return False
            job.state, job.last_error = plan.state, error
            job.leased_by = job.lease_expires_at = None
            if plan.retry_at is not None:
                job.run_at = plan.retry_at
            job.finished_at = now if plan.state is JobState.DEAD else None
            return True

    def release_expired(self, *, now: datetime) -> int:
        with self._guard:
            expired = [
                j
                for j in self.jobs.values()
                if j.state is JobState.RUNNING
                and j.lease_expires_at is not None
                and j.lease_expires_at < now
            ]
            for job in expired:
                dead = job.attempts >= job.spec.retry.max_attempts
                job.state = JobState.DEAD if dead else JobState.RETRY_SCHEDULED
                job.finished_at = now if dead else None
                job.run_at = now
                job.leased_by = job.lease_expires_at = None
                job.last_error = "LeaseExpired: the worker stopped before finishing the job"
            return len(expired)

    def dead_letters(self, *, organization_id: uuid.UUID | None, limit: int) -> list[DeadLetter]:
        dead = [
            j
            for j in self.jobs.values()
            if j.state is JobState.DEAD
            and (organization_id is None or j.spec.organization_id == organization_id)
        ]
        dead.sort(key=lambda j: (j.finished_at, j.id), reverse=True)
        return [
            DeadLetter(
                id=j.id,
                kind=j.spec.kind,
                organization_id=j.spec.organization_id,
                shop_id=j.spec.shop_id,
                attempts=j.attempts,
                last_error=j.last_error,
                created_at=j.created_at,
                failed_at=j.finished_at or j.created_at,
            )
            for j in dead[:limit]
        ]

    def prune_finished(self, *, before: datetime) -> int:
        with self._guard:
            old = [
                job_id
                for job_id, j in self.jobs.items()
                if j.state is JobState.SUCCEEDED and j.finished_at and j.finished_at < before
            ]
            for job_id in old:
                del self.jobs[job_id]
            return len(old)

    # --- test helpers ---

    def only(self, kind: str | None = None) -> StoredJob:
        [job] = [j for j in self.jobs.values() if kind is None or j.spec.kind == kind]
        return job

    def of_kind(self, kind: str) -> list[StoredJob]:
        return [j for j in self.jobs.values() if j.spec.kind == kind]

    def _held(self, job_id: uuid.UUID, worker_id: str) -> StoredJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.state is not JobState.RUNNING or job.leased_by != worker_id:
            return None
        return job

    @staticmethod
    def _claimed(job: StoredJob) -> ClaimedJob:
        assert job.lease_expires_at is not None
        return ClaimedJob(
            id=job.id,
            kind=job.spec.kind,
            idempotency_key=job.spec.idempotency_key,
            payload=dict(job.spec.payload),
            organization_id=job.spec.organization_id,
            shop_id=job.spec.shop_id,
            lock_key=job.spec.lock_key,
            attempts=job.attempts,
            retry=job.spec.retry,
            lease_expires_at=job.lease_expires_at,
            correlation_id=job.spec.correlation_id,
            causation_id=job.spec.causation_id,
        )


@dataclass
class MemoryHeartbeats:
    beats: list[dict[str, object]] = field(default_factory=list)

    def beat(  # noqa: PLR0913 - mirrors the port
        self,
        *,
        worker_id: str,
        started_at: datetime,
        now: datetime,
        current_job_id: uuid.UUID | None,
        release: str | None,
        stopping: bool,
    ) -> None:
        self.beats.append(
            {
                "worker_id": worker_id,
                "started_at": started_at,
                "now": now,
                "current_job_id": current_job_id,
                "release": release,
                "stopping": stopping,
            }
        )


def a_job(kind: str = "test.work", key: str = "k-1", **changes: object) -> JobSpec:
    return replace(JobSpec(kind=kind, idempotency_key=key), **changes)  # type: ignore[arg-type]
