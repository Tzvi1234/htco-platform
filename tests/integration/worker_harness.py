"""A real worker process for the crash and shutdown tests (slice 5.1):

    python -m tests.integration.worker_harness <kind> <lease seconds> <grace seconds>

It runs the production `Worker` loop and signal handling on the worker role against
`HTCO_TEST_DATABASE_URL`, with one test handler for `<kind>`: it records `started`, then holds
the job forever when the payload says `hold_first` and this is the first attempt (the test kills
the process there), or sleeps `seconds`; then it records the business effect once per job (a
unique insert, like every real handler's idempotent write) and `finished`.
"""

import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

import anyio

from htco_application.jobs import ClaimedJob
from htco_persistence.database import Database, tenant_scope
from htco_persistence.jobs import PostgresJobQueue, PostgresWorkerHeartbeats
from htco_platform.logs import configure_logging
from htco_worker.main import WORKER_ROLE, stop_on_signal
from htco_worker.runner import Worker, WorkerConfig

MARKS_TABLE = "public.job_test_marks"


def _mark(database: Database, job: ClaimedJob, phase: str) -> None:
    with database.connection() as connection, connection.transaction():
        connection.execute(
            f"insert into {MARKS_TABLE} (job_id, attempt, phase, pid) values (%s, %s, %s, %s) "  # noqa: S608 - constant table
            "on conflict do nothing",
            [job.id, job.attempts, phase, os.getpid()],
        )


async def _serve(kind: str, lease: float, grace: float) -> None:
    database = Database(os.environ["HTCO_TEST_DATABASE_URL"], max_size=3, role=WORKER_ROLE)

    async def handle(job: ClaimedJob) -> None:
        await anyio.to_thread.run_sync(_mark, database, job, "started")
        if job.payload.get("hold_first") and job.attempts == 1:
            await anyio.sleep_forever()
        seconds = job.payload.get("seconds", 0)
        await anyio.sleep(float(seconds) if isinstance(seconds, int | float) else 0.0)
        await anyio.to_thread.run_sync(_mark, database, job, "effect")
        await anyio.to_thread.run_sync(_mark, database, job, "finished")

    worker = Worker(
        PostgresJobQueue(database),
        PostgresWorkerHeartbeats(database),
        {kind: handle},
        config=WorkerConfig(
            worker_id=f"harness-{os.getpid()}-{uuid.uuid4().hex[:6]}",
            lease=timedelta(seconds=lease),
            poll_interval=0.1,
            shutdown_grace=grace,
        ),
        clock=lambda: datetime.now(UTC),
        jitter=random.random,
        tenant=tenant_scope,
    )
    await database.start()
    stop = anyio.Event()
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(stop_on_signal, stop)
            await worker.run(stop)
            group.cancel_scope.cancel()
    finally:
        await database.stop()


def main(argv: list[str]) -> int:
    kind, lease, grace = argv[0], float(argv[1]), float(argv[2])
    configure_logging(level="INFO")
    anyio.run(_serve, kind, lease, grace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
