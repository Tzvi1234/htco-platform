"""A real worker process for the crash and shutdown tests (slice 5.1):

    python -m tests.integration.worker_harness <kind> <lease seconds> <grace seconds> [outbox]

It runs the production `Worker` loop and signal handling on the worker role against
`HTCO_TEST_DATABASE_URL`, with one test handler for `<kind>`: it records `started`, then holds
the job forever when the payload says `hold_first` and this is the first attempt (the test kills
the process there), or sleeps `seconds`; then it records the business effect once per job (a
unique insert, like every real handler's idempotent write) and `finished`.

With `outbox` it runs the production `outbox.deliver` job instead (slice 5.2), delivering to a
test destination that holds its first delivery forever when the payload says `hold_first` and
records each delivery once per idempotency key, as a deduplicating destination does.

With `automation <automation id>` it runs the production `automation.execute` job (slice 5.10) with
one test handler for that automation, which holds its first run forever when the payload says
`hold_first` and records its business effect once per `mark_id`.
"""

import os
import random
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import anyio

from htco_application.automation.durable import (
    Completed,
    DurableAutomationEngine,
    HandlerContext,
    HandlerOutcome,
)
from htco_application.automation.records import registry_from_rows
from htco_application.execution.gateway import UnavailableGateway
from htco_application.jobs import ClaimedJob
from htco_application.messaging import PendingDelivery
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from htco_persistence.jobs import PostgresJobQueue, PostgresWorkerHeartbeats
from htco_persistence.messaging import PostgresOutbox
from htco_platform.logs import configure_logging
from htco_worker.main import WORKER_ROLE, automation_handlers, outbox_handlers, stop_on_signal
from htco_worker.runner import JobHandler, Worker, WorkerConfig

MARKS_TABLE = "public.job_test_marks"


def _mark(database: Database, job: ClaimedJob, phase: str) -> None:
    with database.connection() as connection, connection.transaction():
        connection.execute(
            f"insert into {MARKS_TABLE} (job_id, attempt, phase, pid) values (%s, %s, %s, %s) "  # noqa: S608 - constant table
            "on conflict do nothing",
            [job.id, job.attempts, phase, os.getpid()],
        )


class HoldingSink:
    def __init__(self, database: Database) -> None:
        self._database = database

    def _record(self, delivery: PendingDelivery, phase: str) -> int:
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                f"select count(*) from {MARKS_TABLE} where job_id = %s and phase = 'started'",  # noqa: S608 - constant table
                [delivery.id],
            ).fetchone()
            seen = 0 if row is None else int(row[0])
            connection.execute(
                f"insert into {MARKS_TABLE} (job_id, attempt, phase, pid) "  # noqa: S608 - constant table
                "values (%s, %s, %s, %s) on conflict do nothing",
                [delivery.id, seen + 1 if phase == "started" else seen, phase, os.getpid()],
            )
        return seen

    async def deliver(self, delivery: PendingDelivery) -> None:
        before = await anyio.to_thread.run_sync(self._record, delivery, "started")
        if delivery.message.payload.get("hold_first") and before == 0:
            await anyio.sleep_forever()
        await anyio.to_thread.run_sync(self._record, delivery, "effect")


class HoldingAutomation:
    """A handler: idempotent effect per `mark_id`; holds its first run when asked."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def __call__(self, context: HandlerContext) -> HandlerOutcome:
        return await anyio.to_thread.run_sync(self._run, context.payload)

    def _run(self, payload: Mapping[str, object]) -> HandlerOutcome:
        mark = uuid.UUID(str(payload["mark_id"]))
        with self._database.connection() as connection, connection.transaction():
            row = connection.execute(
                f"select count(*) from {MARKS_TABLE} where job_id = %s and phase = 'started'",  # noqa: S608 - constant table
                [mark],
            ).fetchone()
            seen = 0 if row is None else int(row[0])
            connection.execute(
                f"insert into {MARKS_TABLE} (job_id, attempt, phase, pid) "  # noqa: S608 - constant table
                "values (%s, %s, 'started', %s)",
                [mark, seen + 1, os.getpid()],
            )
        if payload.get("hold_first") and seen == 0:
            time.sleep(3600)  # killed here
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                f"insert into {MARKS_TABLE} (job_id, attempt, phase, pid) "  # noqa: S608 - constant table
                "values (%s, %s, 'effect', %s) on conflict do nothing",
                [mark, seen + 1, os.getpid()],
            )
        return Completed({"done": True})


async def _serve(kind: str, lease: float, grace: float, mode: str) -> None:
    database = Database(os.environ["HTCO_TEST_DATABASE_URL"], max_size=3, role=WORKER_ROLE)

    async def handle(job: ClaimedJob) -> None:
        await anyio.to_thread.run_sync(_mark, database, job, "started")
        if job.payload.get("hold_first") and job.attempts == 1:
            await anyio.sleep_forever()
        seconds = job.payload.get("seconds", 0)
        await anyio.sleep(float(seconds) if isinstance(seconds, int | float) else 0.0)
        await anyio.to_thread.run_sync(_mark, database, job, "effect")
        await anyio.to_thread.run_sync(_mark, database, job, "finished")

    handlers: dict[str, JobHandler] = {kind: handle}
    if mode == "outbox":
        handlers = outbox_handlers(PostgresOutbox(database), {"test.sink": HoldingSink(database)})
    elif mode.startswith("automation:"):
        catalog = PostgresAutomationCatalog(database)
        engine = DurableAutomationEngine(
            lambda: registry_from_rows(catalog.current()),
            PostgresExecutionStore(database),
            {mode.removeprefix("automation:"): HoldingAutomation(database)},
            clock=lambda: datetime.now(UTC),
            jitter=random.random,
            gateway=UnavailableGateway(),
            offload=anyio.to_thread.run_sync,
        )
        handlers = automation_handlers(engine, f"harness-{os.getpid()}")
    worker = Worker(
        PostgresJobQueue(database),
        PostgresWorkerHeartbeats(database),
        handlers,
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
    mode = argv[3] if len(argv) > 3 else "job"
    configure_logging(level="INFO")
    anyio.run(_serve, kind, lease, grace, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
