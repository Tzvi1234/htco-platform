"""Browser dispatch on Postgres, the browser worker's side (slice 8A.2c).

Every call goes through the `browser` schema's security-definer functions, on a
`Database(role="htco_browser")`: the browser role has no right on the queue, the bindings, the
outcomes or the executions themselves. The organization, the execution and the environment come
from the leased job row inside the database, never from here. Each method hops to a thread
(AUD-05). The start and the unapproved read run on the profile lock's own connection, so the
database can check that this backend holds the lock (AC-8A.2.28).
"""

from datetime import datetime, timedelta
from typing import Any

import anyio
import psycopg
from psycopg.types.json import Jsonb

from htco_application.browser_dispatch import (
    BrowserJobLease,
    LeaseLostError,
    RefusedRun,
    RunBinding,
    RunOutcome,
    StartedRun,
)
from htco_persistence.browser_sessions import PostgresLease
from htco_persistence.database import Database


def _seconds(lease: timedelta) -> int:
    return max(1, round(lease.total_seconds()))


class PostgresBrowserJobs:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, *, worker_id: str, lease: timedelta) -> BrowserJobLease | None:
        def run() -> BrowserJobLease | None:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    "select job_id, kind, organization_id, shop_id, payload, attempts, "
                    "lease_token, lease_expires_at from browser.claim_job(%s, %s)",
                    [worker_id, _seconds(lease)],
                ).fetchone()
            if row is None:
                return None
            return BrowserJobLease(
                job_id=row[0], kind=row[1], organization_id=row[2], shop_id=row[3],
                payload=row[4], attempts=row[5], token=row[6], lease_expires_at=row[7],
            )  # fmt: skip

        return await anyio.to_thread.run_sync(run)

    async def renew(self, job: BrowserJobLease, *, lease: timedelta) -> datetime:
        def run() -> datetime:
            try:
                with self._database.connection() as connection, connection.transaction():
                    row = connection.execute(
                        "select browser.renew_lease(%s, %s, %s)",
                        [job.job_id, job.token, _seconds(lease)],
                    ).fetchone()
            except psycopg.errors.InsufficientPrivilege:
                raise LeaseLostError("the job's lease is gone") from None
            if row is None or row[0] is None:  # pragma: no cover - the function returns one row
                raise LeaseLostError("the job's lease is gone")
            until: datetime = row[0]
            return until

        return await anyio.to_thread.run_sync(run)

    async def complete(self, job: BrowserJobLease) -> None:
        await self._call("select browser.complete_job(%s, %s)", [job.job_id, job.token])

    async def fail(self, job: BrowserJobLease, *, error: str, retry_at: datetime | None) -> None:
        await self._call(
            "select browser.fail_job(%s, %s, %s, %s)", [job.job_id, job.token, error, retry_at]
        )

    async def _call(self, statement: str, params: list[object]) -> None:
        def run() -> None:
            try:
                with self._database.connection() as connection, connection.transaction():
                    connection.execute(statement, params)
            except psycopg.errors.InsufficientPrivilege:
                raise LeaseLostError("the job's lease is gone") from None

        await anyio.to_thread.run_sync(run)


def _lock_connection(on: object) -> psycopg.Connection[Any]:
    if not isinstance(on, PostgresLease) or not on.held:
        raise LeaseLostError("a run starts only under its profile lock")
    return on.connection


class PostgresBrowserRuns:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def start(self, job: BrowserJobLease, *, on: object) -> StartedRun | RefusedRun | None:
        connection = _lock_connection(on)

        def run() -> tuple[Any, ...] | None:
            with self._database.transaction_on(connection):
                return connection.execute(
                    "select outcome, refusal, run_token, action_execution_id, attempt, "
                    "environment_id, action_name, recipe_id, recipe_version, scope_id, "
                    "parameters, request_hash, is_write, deadline "
                    "from browser.start_run(%s, %s)",
                    [job.job_id, job.token],
                ).fetchone()

        try:
            row = await anyio.to_thread.run_sync(run)
        except psycopg.errors.InsufficientPrivilege:
            raise LeaseLostError("the job is not leased with this token") from None
        if row is None:
            return None
        if row[0] == "refused":
            return RefusedRun(action_execution_id=row[3], attempt=row[4], code=row[1])
        binding = RunBinding(
            action_execution_id=row[3], attempt=row[4], environment_id=row[5],
            action_name=row[6], recipe_id=row[7], recipe_version=row[8], scope_id=row[9],
            parameters=row[10], request_hash=row[11], is_write=row[12], deadline=row[13],
        )  # fmt: skip
        return StartedRun(binding=binding, run_token=row[2])

    async def finish(self, run_token: str, outcome: RunOutcome) -> bool:
        def run() -> bool:
            with self._database.connection() as connection, connection.transaction():
                row = connection.execute(
                    "select browser.finish_run(%s, %s, %s, %s, %s)",
                    _outcome_params(run_token, outcome),
                ).fetchone()
            return bool(row and row[0])

        return await anyio.to_thread.run_sync(run)

    async def record_unapproved_read(
        self, job: BrowserJobLease, outcome: RunOutcome, *, on: object
    ) -> bool:
        connection = _lock_connection(on)

        def run() -> bool:
            with self._database.transaction_on(connection):
                row = connection.execute(
                    "select browser.record_unapproved_read(%s, %s, %s, %s, %s, %s)",
                    [job.job_id, *_outcome_params(job.token, outcome)],
                ).fetchone()
            return bool(row and row[0])

        try:
            return await anyio.to_thread.run_sync(run)
        except psycopg.errors.InsufficientPrivilege:
            raise LeaseLostError("the job is not leased with this token") from None


def _outcome_params(token: str, outcome: RunOutcome) -> list[object]:
    return [
        token,
        outcome.status,
        outcome.error_code,
        None if outcome.output is None else Jsonb(dict(outcome.output)),
        Jsonb(list(outcome.evidence)),
    ]
