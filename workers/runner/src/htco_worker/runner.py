"""The worker loop (slice 5.1): claim, heartbeat, run, complete or fail; schedules; shutdown.

One job at a time per worker process (the pilot needs no more; add processes, not threads). Each
poll enqueues the current period's job of every enabled schedule (a no-op after the first worker
did it), releases jobs whose worker died (their lease expired), then claims the next due job of a
kind this worker handles. While a job runs, the heartbeat extends its lease and records the
worker's liveness in `jobs.worker_heartbeats`.

Stopping (SIGTERM) claims nothing new and lets the current job finish within the grace period
(AC-5.1.4). A job still running at the deadline is abandoned without completing: its lease
expires and another worker runs it again, which every handler tolerates (ER-REL-01). Database
calls run in threads that a cancellation cannot interrupt, so if the loop has still not stopped a
few seconds after the deadline, `on_overrun` ends the process (the composition root exits
immediately; the platform would kill it soon anyway).
"""

import math
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

import anyio
import structlog

from htco_application.jobs import ClaimedJob, JobQueue, WorkerHeartbeats, due_schedule_jobs
from htco_application.kernel import describe_error
from htco_domain.jobs import JobState, Schedule, plan_after_failure

type JobHandler = Callable[[ClaimedJob], Awaitable[None]]
type TenantScope = Callable[[uuid.UUID | None], AbstractContextManager[None]]

_log = structlog.get_logger(__name__)
# `jobs.jobs.leased_by` and `jobs.worker_heartbeats.worker_id` hold at most this many characters.
MAX_WORKER_ID_LENGTH = 200


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    lease: timedelta = timedelta(seconds=60)
    poll_interval: float = 1.0
    shutdown_grace: float = 20.0
    release: str | None = None
    # Schedules are enqueued at most this often; their keys make extra ticks harmless.
    schedule_interval: timedelta = timedelta(seconds=30)
    # After the grace period, how long blocking work may still hold the loop before `on_overrun`.
    overrun_after: float = 5.0

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > MAX_WORKER_ID_LENGTH:
            raise ValueError(f"a worker needs an ID of 1 to {MAX_WORKER_ID_LENGTH} characters")
        if self.lease < timedelta(seconds=2):
            raise ValueError("a lease of at least 2 seconds")
        if self.poll_interval <= 0 or self.shutdown_grace < 0:
            raise ValueError("the poll interval is positive and the grace period not negative")

    @property
    def heartbeat_interval(self) -> float:
        """Three beats per lease: one missed beat never loses the job."""
        return self.lease.total_seconds() / 3


class Worker:
    def __init__(  # noqa: PLR0913 - the loop's collaborators, all injected
        self,
        queue: JobQueue,
        heartbeats: WorkerHeartbeats,
        handlers: Mapping[str, JobHandler],
        *,
        config: WorkerConfig,
        clock: Callable[[], datetime],
        jitter: Callable[[], float],
        tenant: TenantScope,
        schedules: Sequence[Schedule] = (),
        on_overrun: Callable[[], None] = lambda: None,
    ) -> None:
        missing = sorted({s.kind for s in schedules} - handlers.keys())
        if missing:
            raise ValueError(f"no handler for the schedules {missing}")
        self._queue = queue
        self._heartbeats = heartbeats
        self._handlers = handlers
        self._config = config
        self._clock = clock
        self._jitter = jitter
        self._tenant = tenant
        self._schedules = tuple(schedules)
        self._started_at = clock()
        self._next_schedule_tick: datetime | None = None
        self._current: ClaimedJob | None = None
        self._stopping = False
        self.last_loop_at: datetime | None = None
        self._on_overrun = on_overrun
        self._overrun: threading.Timer | None = None

    async def run(self, stop: anyio.Event) -> None:
        """Process jobs until `stop` is set, then finish the current job within the grace."""
        async with anyio.create_task_group() as group:
            group.start_soon(self._beat_until_cancelled)
            group.start_soon(self._enforce_grace, stop, group.cancel_scope)
            try:
                await self._loop(stop)
            finally:
                group.cancel_scope.cancel()
        if self._overrun is not None:
            self._overrun.cancel()
        self._stopping = True
        await self._beat()
        _log.info("worker.stopped", worker_id=self._config.worker_id)

    async def run_once(self) -> bool:
        """One poll: schedules, expired leases, then at most one job. True when a job ran."""
        now = self._clock()
        await self._enqueue_schedules(now)
        released = await anyio.to_thread.run_sync(lambda: self._queue.release_expired(now=now))
        if released:
            _log.warning("jobs.leases_expired", count=released)
        job = await anyio.to_thread.run_sync(self._claim, now)
        # Alive means the queue answered: a loop whose polls keep failing is reported stalled.
        self.last_loop_at = now
        if job is None:
            return False
        await self._process(job)
        return True

    async def _loop(self, stop: anyio.Event) -> None:
        while not stop.is_set():
            try:
                if await self.run_once():
                    continue
            except Exception as error:
                _log.warning("worker.poll_failed", error=type(error).__name__)
            with anyio.move_on_after(self._config.poll_interval):
                await stop.wait()

    async def _enforce_grace(self, stop: anyio.Event, scope: anyio.CancelScope) -> None:
        await stop.wait()
        self._stopping = True
        if self._current is not None:
            _log.info("worker.stopping", job_id=str(self._current.id), kind=self._current.kind)
        await anyio.sleep(self._config.shutdown_grace)
        if self._current is not None:
            _log.warning(
                "worker.grace_exceeded", job_id=str(self._current.id), kind=self._current.kind
            )
        # A thread the cancellation cannot interrupt may still hold the loop: bound it.
        self._overrun = threading.Timer(self._config.overrun_after, self._overran)
        self._overrun.daemon = True
        self._overrun.start()
        scope.cancel()

    def _overran(self) -> None:
        _log.error("worker.forced_exit", after_seconds=self._config.overrun_after)
        self._on_overrun()

    def _claim(self, now: datetime) -> ClaimedJob | None:
        return self._queue.claim(
            worker_id=self._config.worker_id,
            kinds=list(self._handlers),
            now=now,
            lease=self._config.lease,
        )

    async def _enqueue_schedules(self, now: datetime) -> None:
        if not self._schedules or (
            self._next_schedule_tick is not None and now < self._next_schedule_tick
        ):
            return
        self._next_schedule_tick = now + self._config.schedule_interval
        specs = due_schedule_jobs(self._schedules, now)

        def enqueue_all() -> None:
            for spec in specs:
                self._queue.enqueue(spec, now=now)

        await anyio.to_thread.run_sync(enqueue_all)

    async def _process(self, job: ClaimedJob) -> None:
        self._current = job
        log = _log.bind(
            job_id=str(job.id),
            kind=job.kind,
            attempt=job.attempts,
            correlation_id=job.correlation_id,
            causation_id=job.causation_id,
        )
        log.info("job.started")
        try:
            with self._tenant(job.organization_id):
                await self._handlers[job.kind](job)
        except Exception as error:
            await self._fail(job, error, log)
        else:
            done = await anyio.to_thread.run_sync(
                lambda: self._queue.complete(
                    job.id, worker_id=self._config.worker_id, now=self._clock()
                )
            )
            if done:
                log.info("job.succeeded")
            else:
                log.warning("job.lease_lost")
        finally:
            self._current = None

    async def _fail(
        self, job: ClaimedJob, error: Exception, log: structlog.stdlib.BoundLogger
    ) -> None:
        now = self._clock()
        plan = plan_after_failure(
            attempts=job.attempts, policy=job.retry, now=now, jitter=self._jitter()
        )
        recorded = await anyio.to_thread.run_sync(
            lambda: self._queue.fail(
                job.id,
                worker_id=self._config.worker_id,
                error=describe_error(error),
                plan=plan,
                now=now,
            )
        )
        if not recorded:
            log.warning("job.lease_lost", error=type(error).__name__)
        elif plan.state is JobState.DEAD:
            log.error("job.dead_lettered", error=type(error).__name__)
        else:
            retry_in = 0.0 if plan.retry_at is None else (plan.retry_at - now).total_seconds()
            log.warning("job.retry_scheduled", error=type(error).__name__, retry_in=retry_in)

    async def _beat_until_cancelled(self) -> None:
        while True:
            await self._beat()
            await anyio.sleep(self._config.heartbeat_interval)

    async def _beat(self) -> None:
        now = self._clock()
        current = self._current
        try:
            if current is not None:
                held = await anyio.to_thread.run_sync(
                    lambda: self._queue.extend_lease(
                        current.id,
                        worker_id=self._config.worker_id,
                        now=now,
                        lease=self._config.lease,
                    )
                )
                if not held:
                    _log.warning("job.lease_lost", job_id=str(current.id), kind=current.kind)
            await anyio.to_thread.run_sync(
                lambda: self._heartbeats.beat(
                    worker_id=self._config.worker_id,
                    started_at=self._started_at,
                    now=now,
                    current_job_id=None if current is None else current.id,
                    release=self._config.release,
                    stopping=self._stopping,
                )
            )
        except Exception as error:
            _log.warning("worker.heartbeat_failed", error=type(error).__name__)

    def is_alive(self, now: datetime) -> bool:
        """For the health port: the loop ran recently (a long job counts as alive while its
        lease is being extended)."""
        if self._current is not None:
            return True
        if self.last_loop_at is None:
            return now - self._started_at < timedelta(minutes=1)
        limit = max(60.0, math.ceil(self._config.poll_interval * 10))
        return (now - self.last_loop_at).total_seconds() < limit
