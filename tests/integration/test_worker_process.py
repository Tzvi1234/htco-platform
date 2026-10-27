"""Real worker processes against real Postgres (slice 5.1): killed mid-job, and stopped with
SIGTERM. The processes run `tests/integration/worker_harness.py` (the production loop and
signal handling with one test handler) and are started, killed and signalled by these tests.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from htco_application.automation.durable import EXECUTION_JOB
from htco_application.jobs import JobSpec
from htco_application.kernel import OutboxMessage
from htco_application.messaging import OUTBOX_DELIVERY_JOB
from htco_domain.automation import RetryPolicy
from htco_persistence.database import Database
from htco_persistence.jobs import PostgresJobQueue
from htco_persistence.messaging import PostgresOutbox
from tests.automation_fakes import EffectBook
from tests.integration.test_durable_engine import engine_on, register, trigger
from tests.integration.test_etsy_connection import Tenant, _url, scalar
from tests.integration.worker_harness import MARKS_TABLE

pytestmark = [pytest.mark.db, pytest.mark.anyio]

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRIES = RetryPolicy(max_attempts=3, initial_backoff=timedelta(seconds=1))


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    with db.connection() as connection:
        connection.execute(
            f"""
            create table if not exists {MARKS_TABLE} (
              id uuid primary key default gen_random_uuid(),
              job_id uuid not null,
              attempt integer not null,
              phase text not null,
              pid integer not null,
              at timestamptz not null default now(),
              unique (job_id, attempt, phase)
            );
            create unique index if not exists job_test_marks_one_effect_key
              on {MARKS_TABLE} (job_id) where phase = 'effect';
            grant select, insert on {MARKS_TABLE} to htco_worker;
            """
        )
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Callable[..., subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []

    def start(
        kind: str, *, lease: float, grace: float, mode: str = "job"
    ) -> subprocess.Popen[bytes]:
        with (tmp_path / f"worker-{len(started)}.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.integration.worker_harness",
                    kind,
                    str(lease),
                    str(grace),
                    mode,
                ],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    for log in sorted(tmp_path.glob("worker-*.log")):
        sys.stdout.write(f"--- {log.name}\n{log.read_text(encoding='utf-8', errors='replace')}\n")


def marks(admin: Database, job_id: uuid.UUID, phase: str) -> list[tuple[Any, ...]]:
    with admin.connection() as connection:
        return connection.execute(
            f"select attempt, pid from {MARKS_TABLE} where job_id = %s and phase = %s "  # noqa: S608 - constant table
            "order by attempt",
            [job_id, phase],
        ).fetchall()


def job_row(admin: Database, job_id: uuid.UUID) -> tuple[str, int]:
    with admin.connection() as connection:
        row = connection.execute(
            "select state, attempts from jobs.jobs where id = %s", [job_id]
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def wait_for(condition: Callable[[], bool], *, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {what}")


def enqueue(admin: Database, kind: str, payload: dict[str, Any]) -> uuid.UUID:
    return PostgresJobQueue(admin).enqueue(
        JobSpec(kind=kind, idempotency_key="crash-test", payload=payload, retry=RETRIES),
        now=datetime.now(UTC),
    )


@pytest.mark.ac("AC-5.1.1")
async def test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart(
    admin: Database, spawn: Callable[..., subprocess.Popen[bytes]]
) -> None:
    kind = f"test.k{uuid.uuid4().hex[:12]}"
    job_id = enqueue(admin, kind, {"hold_first": True})
    first = spawn(kind, lease=3, grace=5)
    wait_for(lambda: bool(marks(admin, job_id, "started")), seconds=60, what="the first attempt")

    first.kill()  # SIGKILL: no shutdown, no completion, the lease is left to expire
    first.wait(timeout=10)
    assert job_row(admin, job_id) == ("running", 1)
    second = spawn(kind, lease=3, grace=5)
    wait_for(lambda: job_row(admin, job_id)[0] == "succeeded", seconds=60, what="the retry")
    second.send_signal(signal.SIGTERM)
    second.wait(timeout=30)

    assert job_row(admin, job_id) == ("succeeded", 2)
    assert [attempt for attempt, _ in marks(admin, job_id, "started")] == [1, 2]
    assert len(marks(admin, job_id, "effect")) == 1
    [(_, finished_by)] = marks(admin, job_id, "finished")
    assert finished_by == second.pid


@pytest.mark.ac("AC-5.1.4")
async def test_sigterm__the_current_job_finishes_before_the_worker_exits(
    admin: Database, spawn: Callable[..., subprocess.Popen[bytes]]
) -> None:
    kind = f"test.k{uuid.uuid4().hex[:12]}"
    job_id = enqueue(admin, kind, {"seconds": 2})
    worker = spawn(kind, lease=10, grace=15)
    wait_for(lambda: bool(marks(admin, job_id, "started")), seconds=60, what="the job to start")

    worker.send_signal(signal.SIGTERM)
    code = worker.wait(timeout=30)

    assert code == 0
    assert job_row(admin, job_id) == ("succeeded", 1)
    assert len(marks(admin, job_id, "effect")) == 1


@pytest.mark.ac("AC-5.1.4")
async def test_sigterm__a_job_beyond_the_grace_period__is_left_to_its_lease(
    admin: Database, spawn: Callable[..., subprocess.Popen[bytes]]
) -> None:
    kind = f"test.k{uuid.uuid4().hex[:12]}"
    job_id = enqueue(admin, kind, {"seconds": 120})
    worker = spawn(kind, lease=10, grace=1)
    wait_for(lambda: bool(marks(admin, job_id, "started")), seconds=60, what="the job to start")

    started = time.monotonic()
    worker.send_signal(signal.SIGTERM)
    code = worker.wait(timeout=30)

    assert code == 0
    assert time.monotonic() - started < 15
    assert job_row(admin, job_id) == ("running", 1)  # another worker takes it after the lease
    assert marks(admin, job_id, "effect") == []


@pytest.mark.ac("AC-5.2.2")
async def test_crash_after_the_business_commit__the_effect_is_delivered_exactly_once(
    admin: Database, spawn: Callable[..., subprocess.Popen[bytes]]
) -> None:
    tenant = Tenant(admin)
    # The business transaction commits the effect's intent and its delivery job together.
    message_id = PostgresOutbox(admin).add(
        OutboxMessage(
            organization_id=tenant.organization_id,
            destination="test.sink",
            idempotency_key=f"sink:{uuid.uuid4()}",
            payload={"hold_first": True},
        ),
        now=datetime.now(UTC),
    )
    first = spawn(OUTBOX_DELIVERY_JOB, lease=3, grace=5, mode="outbox")
    wait_for(lambda: bool(marks(admin, message_id, "started")), seconds=60, what="delivery")

    first.kill()  # before the destination was reached
    first.wait(timeout=10)
    assert marks(admin, message_id, "effect") == []
    second = spawn(OUTBOX_DELIVERY_JOB, lease=3, grace=5, mode="outbox")
    wait_for(
        lambda: (
            scalar(admin, "select status from jobs.outbox where id = %s", [message_id])
            == "delivered"
        ),
        seconds=60,
        what="the delivery after restart",
    )
    second.send_signal(signal.SIGTERM)
    second.wait(timeout=30)

    assert len(marks(admin, message_id, "effect")) == 1
    assert len(marks(admin, message_id, "started")) == 2


@pytest.mark.ac("AC-5.10.5")
async def test_worker_killed_during_a_running_execution__one_success_and_one_effect(
    admin: Database, spawn: Callable[..., subprocess.Popen[bytes]]
) -> None:
    tenant = Tenant(admin)
    automation = register(admin)
    mark = uuid.uuid4()
    request = trigger(automation, tenant)
    # The trigger side only checks that a handler is deployed; the worker process runs its own.
    execution = engine_on(admin, automation, EffectBook().handle).trigger(
        replace(request, payload={"hold_first": True, "mark_id": str(mark)})
    )
    first = spawn(EXECUTION_JOB, lease=3, grace=5, mode=f"automation:{automation}")
    wait_for(lambda: bool(marks(admin, mark, "started")), seconds=60, what="the first run")

    first.kill()
    first.wait(timeout=10)
    assert (
        scalar(admin, "select state from app.automation_executions where id = %s", [execution.id])
        == "running"
    )
    second = spawn(EXECUTION_JOB, lease=3, grace=5, mode=f"automation:{automation}")
    wait_for(
        lambda: (
            scalar(
                admin, "select state from app.automation_executions where id = %s", [execution.id]
            )
            == "succeeded"
        ),
        seconds=90,
        what="the execution to succeed after the restart",
    )
    second.send_signal(signal.SIGTERM)
    second.wait(timeout=30)

    assert len(marks(admin, mark, "effect")) == 1
    assert (
        scalar(
            admin, "select attempts from app.automation_executions where id = %s", [execution.id]
        )
        == 2
    )
    assert (
        scalar(
            admin,
            "select count(*) from app.automation_execution_events "
            "where execution_id = %s and to_state = 'succeeded'",
            [execution.id],
        )
        == 1
    )
