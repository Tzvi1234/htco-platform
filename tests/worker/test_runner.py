"""The worker loop (slice 5.1) against the in-memory queue: runs, retries, dead letters,
schedules, tenant scope, heartbeats and graceful shutdown.

The same rules are proven against Postgres, with real worker processes that are killed and sent
SIGTERM, in tests/integration/test_job_queue.py and tests/integration/test_worker_process.py.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import override

import anyio
import pytest

from htco_application.jobs import ClaimedJob
from htco_application.kernel import JobError
from htco_domain.automation import RetryPolicy
from htco_domain.jobs import JobState, Schedule
from htco_worker.runner import JobHandler, Worker, WorkerConfig
from tests.job_fakes import MemoryHeartbeats, MemoryJobQueue, a_job

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("e0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("e3000000-0000-0000-0000-000000000001")
START = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TWO_TRIES = RetryPolicy(
    max_attempts=2, initial_backoff=timedelta(seconds=10), max_backoff=timedelta(minutes=1)
)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Tenants:
    """Records the organization each job ran in (the persistence adapter's context variable)."""

    def __init__(self) -> None:
        self.current: uuid.UUID | None = None
        self.seen: list[uuid.UUID | None] = []

    @contextmanager
    def __call__(self, organization_id: uuid.UUID | None) -> Iterator[None]:
        previous, self.current = self.current, organization_id
        self.seen.append(organization_id)
        try:
            yield
        finally:
            self.current = previous


def worker(  # noqa: PLR0913 - test knobs
    queue: MemoryJobQueue,
    handlers: dict[str, JobHandler],
    *,
    clock: Clock | None = None,
    tenants: Tenants | None = None,
    schedules: tuple[Schedule, ...] = (),
    worker_id: str = "worker-a",
    lease: timedelta = timedelta(seconds=30),
    grace: float = 5.0,
    heartbeats: MemoryHeartbeats | None = None,
) -> Worker:
    return Worker(
        queue,
        heartbeats or MemoryHeartbeats(),
        handlers,
        config=WorkerConfig(
            worker_id=worker_id,
            lease=lease,
            poll_interval=0.01,
            shutdown_grace=grace,
            release="test",
        ),
        clock=clock or (lambda: datetime.now(UTC)),
        jitter=lambda: 0.0,
        tenant=tenants or Tenants(),
        schedules=schedules,
    )


async def ok(_: ClaimedJob) -> None:
    return None


async def boom(_: ClaimedJob) -> None:
    raise RuntimeError("buyer FAKE-BUYER-NAME-ZZZ")


async def test_run_once__nothing_due__returns_false() -> None:
    assert await worker(MemoryJobQueue(), {"test.work": ok}).run_once() is False


async def test_run_once__a_due_job__runs_and_succeeds() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(), now=clock.now)

    ran = await worker(queue, {"test.work": ok}, clock=clock).run_once()

    job = queue.only()
    assert ran is True
    assert (job.state, job.attempts, job.finished_at) == (JobState.SUCCEEDED, 1, START)


async def test_run_once__a_job_due_later__waits() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(run_at=START + timedelta(minutes=5)), now=clock.now)

    assert await worker(queue, {"test.work": ok}, clock=clock).run_once() is False


async def test_run_once__kinds_without_a_handler__are_never_claimed() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(kind="other.work"), now=clock.now)

    assert await worker(queue, {"test.work": ok}, clock=clock).run_once() is False
    assert queue.only().state is JobState.QUEUED


async def test_failed_job__is_retried_after_the_jittered_backoff() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(retry=TWO_TRIES), now=clock.now)

    await worker(queue, {"test.work": boom}, clock=clock).run_once()

    job = queue.only()
    # jitter 0: half of the first backoff (10 s).
    assert (job.state, job.run_at) == (JobState.RETRY_SCHEDULED, START + timedelta(seconds=5))
    assert job.last_error == "RuntimeError"


@pytest.mark.ac("AC-5.1.3")
async def test_job_failing_every_attempt__ends_dead_with_its_last_error_and_no_buyer_data() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(retry=TWO_TRIES, organization_id=ORG), now=clock.now)
    loop = worker(queue, {"test.work": boom}, clock=clock)

    await loop.run_once()
    clock.now += timedelta(seconds=10)
    await loop.run_once()

    job = queue.only()
    assert (job.state, job.attempts, job.last_error) == (JobState.DEAD, 2, "RuntimeError")
    [dead] = queue.dead_letters(organization_id=ORG, limit=10)
    assert (dead.kind, dead.attempts, dead.last_error) == ("test.work", 2, "RuntimeError")
    assert queue.dead_letters(organization_id=uuid.uuid4(), limit=10) == []


async def test_job_error__keeps_its_safe_message() -> None:
    async def refused(_: ClaimedJob) -> None:
        raise JobError("shop not connected")

    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(), now=clock.now)

    await worker(queue, {"test.work": refused}, clock=clock).run_once()

    assert queue.only().last_error == "JobError: shop not connected"


async def test_job__runs_inside_its_organization_and_leaves_it_afterwards() -> None:
    queue, clock, tenants = MemoryJobQueue(), Clock(), Tenants()
    seen: list[uuid.UUID | None] = []

    async def record(_: ClaimedJob) -> None:
        seen.append(tenants.current)

    queue.enqueue(a_job(organization_id=ORG, shop_id=SHOP), now=clock.now)
    queue.enqueue(a_job(key="system"), now=clock.now + timedelta(seconds=1))
    loop = worker(queue, {"test.work": record}, clock=clock, tenants=tenants)

    clock.now += timedelta(seconds=1)
    await loop.run_once()
    await loop.run_once()

    assert seen == [ORG, None]
    assert tenants.current is None


@pytest.mark.ac("AC-5.1.1")
async def test_job_of_a_crashed_worker__is_released_after_its_lease_and_runs_once_more() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(), now=clock.now)
    # Worker A claimed the job and vanished (no complete, no fail).
    assert queue.claim(worker_id="a", kinds=["test.work"], now=clock.now, lease=timedelta(30))
    runs: list[int] = []

    async def count(job: ClaimedJob) -> None:
        runs.append(job.attempts)

    survivor = worker(queue, {"test.work": count}, clock=clock, worker_id="b")
    assert await survivor.run_once() is False  # the lease still holds

    clock.now += timedelta(days=31)
    assert await survivor.run_once() is True

    job = queue.only()
    assert (job.state, runs) == (JobState.SUCCEEDED, [2])


async def test_crash_on_the_last_attempt__ends_dead() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(retry=RetryPolicy(1, timedelta(seconds=1))), now=clock.now)
    queue.claim(worker_id="a", kinds=["test.work"], now=clock.now, lease=timedelta(seconds=5))
    clock.now += timedelta(seconds=6)

    await worker(queue, {"test.work": ok}, clock=clock).run_once()

    job = queue.only()
    assert job.state is JobState.DEAD
    assert job.last_error is not None
    assert job.last_error.startswith("LeaseExpired")


async def test_completion_after_the_lease_was_lost__changes_nothing() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    queue.enqueue(a_job(), now=clock.now)

    async def slow_and_overtaken(_: ClaimedJob) -> None:
        # Meanwhile the lease expired and the job was released to another worker.
        clock.now += timedelta(minutes=10)
        queue.release_expired(now=clock.now)

    await worker(queue, {"test.work": slow_and_overtaken}, clock=clock).run_once()

    assert queue.only().state is JobState.RETRY_SCHEDULED


@pytest.mark.ac("AC-5.1.6")
async def test_schedules__two_workers_in_one_period__enqueue_and_run_one_job() -> None:
    queue, clock = MemoryJobQueue(), Clock()
    daily = Schedule("maintenance.capability_probes", timedelta(days=1))
    runs: list[str] = []

    async def fan_out(job: ClaimedJob) -> None:
        runs.append(job.idempotency_key)

    handlers: dict[str, JobHandler] = {daily.kind: fan_out}
    first = worker(queue, handlers, clock=clock, schedules=(daily,), worker_id="a")
    second = worker(queue, handlers, clock=clock, schedules=(daily,), worker_id="b")

    await first.run_once()
    clock.now += timedelta(hours=2)
    await second.run_once()
    await first.run_once()

    assert runs == ["maintenance.capability_probes@2026-10-22T00:00Z"]
    assert len(queue.of_kind(daily.kind)) == 1


def test_worker__a_schedule_without_its_handler__is_refused() -> None:
    with pytest.raises(ValueError, match="no handler"):
        worker(
            MemoryJobQueue(),
            {},
            schedules=(Schedule("maintenance.capability_probes", timedelta(days=1)),),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"worker_id": " "}, "ID"),
        ({"lease": timedelta(seconds=1)}, "lease"),
        ({"poll_interval": 0}, "poll"),
        ({"shutdown_grace": -1}, "grace"),
    ],
)
def test_worker_config__invalid__is_refused(changes: dict[str, object], message: str) -> None:
    settings: dict[str, object] = {"worker_id": "w"} | changes
    with pytest.raises(ValueError, match=message):
        WorkerConfig(**settings)  # type: ignore[arg-type]


@pytest.mark.ac("AC-5.1.2")
async def test_two_workers__never_run_one_job_twice_or_one_lock_at_once() -> None:
    queue = MemoryJobQueue()
    now = datetime.now(UTC)
    for n in range(12):
        queue.enqueue(a_job(key=f"job-{n}", lock_key=f"shop:{n % 2}"), now=now)
    running: dict[str, str] = {}
    overlaps: list[str] = []
    runs: list[uuid.UUID] = []

    async def hold_the_lock(job: ClaimedJob) -> None:
        assert job.lock_key is not None
        if job.lock_key in running:
            overlaps.append(job.lock_key)
        running[job.lock_key] = job.idempotency_key
        await anyio.sleep(0.01)
        del running[job.lock_key]
        runs.append(job.id)

    workers = [worker(queue, {"test.work": hold_the_lock}, worker_id=f"w{n}") for n in range(3)]
    stop = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            for each in workers:
                group.start_soon(each.run, stop)
            while len(runs) < 12:  # noqa: ASYNC110 - polling a test counter
                await anyio.sleep(0.01)
            stop.set()

    assert overlaps == []
    assert sorted(runs) == sorted(queue.jobs)
    assert {j.state for j in queue.jobs.values()} == {JobState.SUCCEEDED}


@pytest.mark.ac("AC-5.1.4")
async def test_stop__lets_the_current_job_finish_then_exits_and_says_so() -> None:
    queue, heartbeats = MemoryJobQueue(), MemoryHeartbeats()
    queue.enqueue(a_job(), now=datetime.now(UTC))
    started, release = anyio.Event(), anyio.Event()

    async def long_job(_: ClaimedJob) -> None:
        started.set()
        await release.wait()

    loop = worker(queue, {"test.work": long_job}, heartbeats=heartbeats, grace=5.0)
    stop = anyio.Event()
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(loop.run, stop)
            await started.wait()
            stop.set()
            await anyio.sleep(0.05)
            assert queue.only().state is JobState.RUNNING  # still finishing
            release.set()

    assert queue.only().state is JobState.SUCCEEDED
    assert heartbeats.beats[-1]["stopping"] is True
    assert heartbeats.beats[-1]["current_job_id"] is None


@pytest.mark.ac("AC-5.1.4")
async def test_stop__a_job_past_the_grace_period__is_abandoned_to_its_lease() -> None:
    queue = MemoryJobQueue()
    queue.enqueue(a_job(), now=datetime.now(UTC))
    started = anyio.Event()

    async def endless(_: ClaimedJob) -> None:
        started.set()
        await anyio.sleep_forever()

    loop = worker(queue, {"test.work": endless}, grace=0.05)
    stop = anyio.Event()
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(loop.run, stop)
            await started.wait()
            stop.set()

    job = queue.only()
    # Not completed: the lease runs out and another worker runs it again.
    assert (job.state, job.leased_by) == (JobState.RUNNING, "worker-a")


async def test_stop__while_idle__exits_at_once() -> None:
    loop = worker(MemoryJobQueue(), {"test.work": ok})
    stop = anyio.Event()
    stop.set()

    with anyio.fail_after(2):
        await loop.run(stop)


async def test_heartbeat__extends_the_running_jobs_lease_and_records_it() -> None:
    queue, heartbeats = MemoryJobQueue(), MemoryHeartbeats()
    queue.enqueue(a_job(), now=datetime.now(UTC))
    leases: list[datetime] = []

    async def watch_the_lease(job: ClaimedJob) -> None:
        for _ in range(3):
            await anyio.sleep(0.8)
            stored = queue.jobs[job.id]
            assert stored.lease_expires_at is not None
            leases.append(stored.lease_expires_at)

    loop = worker(
        queue,
        {"test.work": watch_the_lease},
        heartbeats=heartbeats,
        lease=timedelta(seconds=2),
    )
    stop = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(loop.run, stop)
            while queue.only().state is not JobState.SUCCEEDED:  # noqa: ASYNC110 - test polling
                await anyio.sleep(0.05)
            stop.set()

    assert leases == sorted(leases)
    assert leases[-1] > leases[0]
    assert any(beat["current_job_id"] is not None for beat in heartbeats.beats)


async def test_poll_failure__is_logged_and_the_loop_keeps_going() -> None:
    class FlakyQueue(MemoryJobQueue):
        failures = 2

        @override
        def release_expired(self, *, now: datetime) -> int:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("database away")
            return super().release_expired(now=now)

    queue = FlakyQueue()
    queue.enqueue(a_job(), now=datetime.now(UTC))
    loop = worker(queue, {"test.work": ok})
    stop = anyio.Event()
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(loop.run, stop)
            while queue.only().state is not JobState.SUCCEEDED:  # noqa: ASYNC110 - test polling
                await anyio.sleep(0.01)
            stop.set()


def test_is_alive__recent_loop_or_running_job__else_stalled() -> None:
    clock = Clock()
    loop = worker(MemoryJobQueue(), {"test.work": ok}, clock=clock)

    assert loop.is_alive(START + timedelta(seconds=30))  # just started
    assert not loop.is_alive(START + timedelta(minutes=2))  # never looped
    loop.last_loop_at = START + timedelta(minutes=2)
    assert loop.is_alive(START + timedelta(minutes=2, seconds=30))
    assert not loop.is_alive(START + timedelta(minutes=4))


def test_worker_config__an_id_longer_than_the_queue_columns__is_refused() -> None:
    with pytest.raises(ValueError, match="200"):
        WorkerConfig(worker_id="w" * 201)


@pytest.mark.ac("AC-5.1.4")
async def test_stop__blocking_work_past_the_deadline__ends_the_process() -> None:
    """Database calls run in threads a cancellation cannot interrupt (Codex review of #53)."""
    queue = MemoryJobQueue()
    queue.enqueue(a_job(), now=datetime.now(UTC))
    started = anyio.Event()
    overran = threading.Event()

    async def blocking(_: ClaimedJob) -> None:
        started.set()
        await anyio.to_thread.run_sync(time.sleep, 1.0)

    loop = Worker(
        queue,
        MemoryHeartbeats(),
        {"test.work": blocking},
        config=WorkerConfig(
            worker_id="w", poll_interval=0.01, shutdown_grace=0.05, overrun_after=0.1
        ),
        clock=lambda: datetime.now(UTC),
        jitter=lambda: 0.0,
        tenant=Tenants(),
        on_overrun=overran.set,
    )
    stop = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(loop.run, stop)
            await started.wait()
            stop.set()

    assert overran.is_set()


async def test_stop__finishing_in_time__never_forces_an_exit() -> None:
    overran = threading.Event()
    loop = Worker(
        MemoryJobQueue(),
        MemoryHeartbeats(),
        {"test.work": ok},
        config=WorkerConfig(worker_id="w", poll_interval=0.01, overrun_after=0.05),
        clock=lambda: datetime.now(UTC),
        jitter=lambda: 0.0,
        tenant=Tenants(),
        on_overrun=overran.set,
    )
    stop = anyio.Event()
    stop.set()

    await loop.run(stop)
    await anyio.sleep(0.1)

    assert not overran.is_set()


async def test_polls_that_keep_failing__the_worker_reports_itself_stalled() -> None:
    class DownQueue(MemoryJobQueue):
        @override
        def release_expired(self, *, now: datetime) -> int:
            raise ConnectionError("database away")

    loop = worker(DownQueue(), {"test.work": ok}, clock=Clock())

    with pytest.raises(ConnectionError):
        await loop.run_once()

    assert loop.last_loop_at is None
    assert not loop.is_alive(START + timedelta(minutes=2))
