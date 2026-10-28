"""Browser dispatch against real Postgres, the browser worker's side (slice 8A.2c): the lease
token and the heartbeat (AC-8A.2.21), the start under the profile lock only and once
(AC-8A.2.19, 2.21, 2.28), the outcome by the run token after the lease expired, and the
fail-closed gates (AC-8A.2.26)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from htco_application.browser_dispatch import (
    BrowserJobLease,
    LeaseLostError,
    RefusedRun,
    RunOutcome,
    StartedRun,
    lease_heartbeat,
)
from htco_persistence.browser_dispatch import PostgresBrowserJobs, PostgresBrowserRuns
from htco_persistence.browser_sessions import PostgresProfileLocks
from htco_persistence.database import Database
from htco_persistence.jobs import PostgresJobQueue
from tests.integration.test_etsy_connection import Tenant, _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

HASH = "b" * 64
# High enough that a test's own job is claimed before anything another test left queued.
PRIORITY = 30_000


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def browser() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4, role="htco_browser")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def a_ready_environment(admin: Database, tenant: Tenant) -> tuple[uuid.UUID, uuid.UUID]:
    shop, environment = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Dispatch test shop', 'etsy', 'active')",
            [shop, tenant.organization_id],
        )
        connection.execute(
            "insert into app.shop_browser_environments (id, organization_id, shop_id, status) "
            "values (%s, %s, %s, 'provisioning')",
            [environment, tenant.organization_id, shop],
        )
        connection.execute(
            "update app.shop_browser_environments set status = 'ready' where id = %s",
            [environment],
        )
    return shop, environment


def enqueue(
    admin: Database,
    organization: uuid.UUID,
    shop: uuid.UUID,
    *,
    kind: str,
    payload: dict[str, object],
) -> uuid.UUID:
    job = uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into jobs.jobs (id, kind, idempotency_key, organization_id, shop_id, payload, "
            "priority, max_attempts, initial_backoff_ms, max_backoff_ms, run_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, 3, 0, 0, now() - interval '1 second')",
            [job, kind, f"test-{job}", organization, shop, Jsonb(payload), PRIORITY],
        )
    return job


async def claim_own(browser: Database, job_id: uuid.UUID, *, lease: timedelta) -> BrowserJobLease:
    """Claim until this test's job comes up (completing any stray browser job on the way)."""
    jobs = PostgresBrowserJobs(browser)
    for _ in range(20):
        found = await jobs.claim(worker_id="test-browser", lease=lease)
        assert found is not None
        if found.job_id == job_id:
            return found
        await jobs.complete(found)  # pragma: no cover - only with leftovers from other runs
    raise AssertionError("the test's job was never claimed")  # pragma: no cover


async def lease_for(
    admin: Database, browser: Database, organization: uuid.UUID, shop: uuid.UUID
) -> BrowserJobLease:
    """A leased `browser.*` job for the shop: what every profile access needs (AC-8A.2.23)."""
    job = enqueue(admin, organization, shop, kind="browser.renew", payload={})
    return await claim_own(browser, job, lease=timedelta(minutes=5))


def dispatched_run(  # noqa: PLR0913 - the run's shape
    admin: Database, tenant: Tenant, shop: uuid.UUID, environment: uuid.UUID, *,
    needs_approval: bool = False, deadline: timedelta = timedelta(hours=1), flag: bool = True,
) -> uuid.UUID:  # fmt: skip
    """A dispatched execution with its stored binding and its `browser.run` job (a locator)."""
    execution = uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.action_executions (id, organization_id, shop_id, action_name, "
            "action_version, idempotency_key, request_hash, state, channel, actor_kind, actor_id, "
            "correlation_id) values (%s, %s, %s, 'autova.order.read', 1, %s, %s, 'dispatched', "
            "'browser', 'system', 'test', 'corr-dispatch')",
            [execution, tenant.organization_id, shop, f"key-{execution}", HASH],
        )
        connection.execute(
            "insert into app.browser_run_bindings (action_execution_id, attempt, organization_id, "
            "shop_id, environment_id, action_name, recipe_id, recipe_version, parameters, "
            "request_hash, needs_approval, is_write, deadline) values (%s, 1, %s, %s, %s, "
            "'autova.order.read', 'autova.order_status', 1, %s, %s, %s, false, now() + %s)",
            [execution, tenant.organization_id, shop, environment, Jsonb({"order": 7}), HASH,
             needs_approval, deadline],
        )  # fmt: skip
        if flag:
            connection.execute(
                "insert into app.browser_run_gates (id, organization_id, shop_id, action_name, "
                "kind, actor_id, correlation_id) values (%s, %s, %s, 'autova.order.read', "
                "'flag_on', 'owner', 'corr-flag')",
                [uuid.uuid4(), tenant.organization_id, shop],
            )
    return execution


def run_job(admin: Database, tenant: Tenant, shop: uuid.UUID, execution: uuid.UUID) -> uuid.UUID:
    return enqueue(
        admin, tenant.organization_id, shop, kind="browser.run",
        payload={"action_execution_id": str(execution), "attempt": 1},
    )  # fmt: skip


@pytest.mark.ac("AC-8A.2.18")
async def test_claim__browser_jobs_only_with_a_token_stored_as_its_hash(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    shop, _ = a_ready_environment(admin, tenant)
    other = enqueue(admin, tenant.organization_id, shop, kind="orders.sync", payload={})
    job = enqueue(admin, tenant.organization_id, shop, kind="browser.renew", payload={})

    lease = await claim_own(browser, job, lease=timedelta(minutes=1))

    assert lease.kind == "browser.renew"
    assert lease.token not in repr(lease)
    assert scalar(admin, "select state from jobs.jobs where id = %s", [other]) == "queued"
    assert scalar(
        admin,
        "select lease_token_hash = sha256(convert_to(%s, 'UTF8')) from jobs.jobs where id = %s",
        [lease.token, job],
    )
    with browser.connection() as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute("select 1 from jobs.jobs")


@pytest.mark.ac("AC-8A.2.21")
async def test_heartbeat__a_run_longer_than_its_lease_is_not_redelivered(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    shop, _ = a_ready_environment(admin, tenant)
    jobs = PostgresBrowserJobs(browser)
    job = enqueue(admin, tenant.organization_id, shop, kind="browser.renew", payload={})
    lease = await claim_own(browser, job, lease=timedelta(seconds=5))
    queue = PostgresJobQueue(admin)

    async with lease_heartbeat(
        jobs, lease, every=timedelta(seconds=1), lease=timedelta(seconds=5)
    ) as beat:
        await asyncio.sleep(7)  # longer than one lease period
        released = queue.release_expired(now=datetime.now(UTC))
        assert scalar(admin, "select state from jobs.jobs where id = %s", [job]) == "running"

    assert beat.beats >= 5
    assert not beat.lost
    assert released >= 0
    await jobs.complete(lease)
    assert scalar(
        admin,
        "select lease_token_hash is null and state = 'succeeded' from jobs.jobs where id = %s",
        [job],
    )  # fmt: skip


@pytest.mark.ac("AC-8A.2.21")
@pytest.mark.ac("AC-8A.2.19")
@pytest.mark.ac("AC-8A.2.28")
async def test_start__under_the_lock_once_and_a_redelivered_job_gets_nothing(
    admin: Database, browser: Database
) -> None:
    tenant = Tenant(admin)
    shop, environment = a_ready_environment(admin, tenant)
    execution = dispatched_run(admin, tenant, shop, environment)
    job = run_job(admin, tenant, shop, execution)
    runs = PostgresBrowserRuns(browser)
    locks = PostgresProfileLocks(browser)
    lease = await claim_own(browser, job, lease=timedelta(seconds=5))

    # Without the profile lock on this connection, the start is refused.
    async with locks.hold(environment, until=datetime.now(UTC) + timedelta(seconds=5)) as held:
        other_connection = await anyio_dedicated(browser)
        try:
            from htco_persistence.browser_sessions import PostgresLease  # noqa: PLC0415

            unlocked = PostgresLease(environment, True, other_connection)
            with pytest.raises(LeaseLostError):
                await runs.start(lease, on=unlocked)
        finally:
            other_connection.close()
        started = await runs.start(lease, on=held)

    assert isinstance(started, StartedRun)
    assert started.binding.action_execution_id == execution
    assert started.binding.parameters == {"order": 7}
    assert started.run_token not in repr(started)

    # The lease runs out (a stopped heartbeat); the queue redelivers the job.
    with admin.connection() as connection:
        connection.execute(
            "update jobs.jobs set lease_expires_at = now() - interval '1 second' where id = %s",
            [job],
        )
    PostgresJobQueue(admin).release_expired(now=datetime.now(UTC))
    with admin.connection() as connection:
        connection.execute("update jobs.jobs set run_at = now() where id = %s", [job])
    again = await claim_own(browser, job, lease=timedelta(seconds=30))
    async with locks.hold(environment, until=datetime.now(UTC) + timedelta(seconds=5)) as held:
        assert await runs.start(again, on=held) is None  # no binding, no token, no page

    # The first run still records its outcome with its run token, once.
    outcome = RunOutcome(status="succeeded", output={"status": "shipped"})
    assert await runs.finish(started.run_token, outcome)
    assert not await runs.finish(started.run_token, outcome)
    assert not await runs.finish("forged", outcome)
    assert (
        scalar(
            admin,
            "select status from app.browser_run_outcomes where action_execution_id = %s",
            [execution],
        )
        == "succeeded"
    )
    assert scalar(
        admin, "select count(*) from jobs.outbox where idempotency_key = %s",
        [f"browser.run_finished.v1:{execution}:1"],
    ) == 1  # fmt: skip


@pytest.mark.ac("AC-8A.2.26")
@pytest.mark.ac("AC-8A.2.15")
@pytest.mark.parametrize(
    ("needs_approval", "flag", "code"),
    [(False, False, "flag_off"), (True, True, "approval_unavailable")],
)
async def test_start__fails_closed_without_a_flag_or_an_approval_table(
    admin: Database, browser: Database, needs_approval: bool, flag: bool, code: str
) -> None:
    tenant = Tenant(admin)
    shop, environment = a_ready_environment(admin, tenant)
    execution = dispatched_run(
        admin, tenant, shop, environment, needs_approval=needs_approval, flag=flag
    )
    lease = await claim_own(
        browser, run_job(admin, tenant, shop, execution), lease=timedelta(seconds=30)
    )
    async with PostgresProfileLocks(browser).hold(
        environment, until=datetime.now(UTC) + timedelta(seconds=5)
    ) as held:
        refused = await PostgresBrowserRuns(browser).start(lease, on=held)

    assert refused == RefusedRun(action_execution_id=execution, attempt=1, code=code)


async def anyio_dedicated(database: Database) -> psycopg.Connection[object]:
    import anyio  # noqa: PLC0415

    return await anyio.to_thread.run_sync(database.dedicated_connection)
