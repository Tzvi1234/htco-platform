"""The job queue, the worker role and scheduled maintenance against real Postgres (slice 5.1).

Each test uses job kinds of its own (`test.k<random>`) and its own organizations, so tests never
claim each other's jobs. The queue runs on the worker's pool (`role="htco_worker"`), as in
production; set-up data is written with the login role.
"""

import os
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from htco_application.audit import AuditRecord
from htco_application.jobs import ClaimedJob, JobSpec
from htco_application.maintenance import (
    SCHEDULER,
    TenantShop,
    check_refresh_token,
    probe_shop_daily,
)
from htco_application.shop_capabilities import CapabilityChecks
from htco_domain.automation import RetryPolicy
from htco_domain.integrations import IntegrationStatus, ShopCapability
from htco_domain.jobs import FailurePlan, JobState, Schedule
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_connect import PostgresShopConnector
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.jobs import PostgresJobQueue, PostgresWorkerHeartbeats
from htco_persistence.maintenance import PostgresPurgeStore, PostgresShopDirectory
from htco_persistence.shop_integrations import PostgresCapabilityStore, PostgresTokenVault
from htco_platform.crypto import EnvelopeSealer, KeyRing
from htco_worker.runner import JobHandler, Worker, WorkerConfig
from tests.etsy_fakes import Pinning, Probe
from tests.integration.test_etsy_connection import Tenant, _url, etsy_ids, request_for, scalar
from tests.integration.test_order_history import TABLES, count, order
from tests.job_fakes import MemoryHeartbeats

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
LEASE = timedelta(seconds=30)


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    """The login role: set-up data and checks."""
    db = Database(_url(), min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=4, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
def kind() -> str:
    return f"test.k{uuid.uuid4().hex[:12]}"


@pytest.fixture
def ring() -> KeyRing:
    return KeyRing((f"k-{uuid.uuid4().hex[:8]}", os.urandom(32)), [])


def job(kind: str, key: str = "one", **changes: object) -> JobSpec:
    values: dict[str, object] = {"kind": kind, "idempotency_key": key} | changes
    return JobSpec(**values)  # type: ignore[arg-type]


def state_of(admin: Database, job_id: uuid.UUID) -> tuple[str, int, str | None]:
    with admin.connection() as connection:
        row = connection.execute(
            "select state, attempts, last_error from jobs.jobs where id = %s", [job_id]
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


async def connected_shop(admin: Database, ring: KeyRing, tenant: Tenant) -> uuid.UUID:
    connected = await PostgresShopConnector(
        admin, PostgresTokenVault(admin, EnvelopeSealer(ring))
    ).connect(request_for(tenant, etsy_ids()))
    return connected.shop.id


def add_synced_rows(admin: Database, organization_id: uuid.UUID, shop_id: uuid.UUID) -> None:
    """Synced data of the shop (probe results stand in for the orders of later slices)."""
    with admin.connection() as connection:
        connection.execute(
            """
            insert into app.shop_capabilities
              (id, organization_id, shop_id, capability, state, detail, checked_at)
            values (%s, %s, %s, 'shop_details', 'available', 'read_ok', now()),
                   (%s, %s, %s, 'listings_read', 'available', 'read_ok', now())
            on conflict (shop_id, capability) do nothing
            """,
            [uuid.uuid7(), organization_id, shop_id, uuid.uuid7(), organization_id, shop_id],
        )


def count_capabilities(admin: Database, shop_id: uuid.UUID) -> int:
    return int(
        scalar(admin, "select count(*) from app.shop_capabilities where shop_id = %s", [shop_id])
    )


# --- Enqueue, claim, complete --------------------------------------------------------------


async def test_enqueue__same_kind_and_key_twice__one_job(worker_db: Database, kind: str) -> None:
    queue = PostgresJobQueue(worker_db)

    first = queue.enqueue(job(kind), now=NOW)
    second = queue.enqueue(job(kind, payload={"other": True}), now=NOW)

    assert first == second
    rows = scalar(worker_db, "select count(*) from jobs.jobs where kind = %s", [kind])
    assert rows == 1


async def test_claim_and_complete__the_job_succeeds_once(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    job_id = queue.enqueue(job(kind, payload={"receipt": 42}), now=NOW)

    claimed = queue.claim(worker_id="w1", kinds=[kind], now=NOW, lease=LEASE)

    assert claimed is not None
    assert (claimed.id, claimed.attempts, claimed.payload) == (job_id, 1, {"receipt": 42})
    assert queue.claim(worker_id="w2", kinds=[kind], now=NOW, lease=LEASE) is None
    assert queue.complete(job_id, worker_id="w1", now=NOW) is True
    assert queue.complete(job_id, worker_id="w1", now=NOW) is False
    assert state_of(admin, job_id) == ("succeeded", 1, None)


async def test_claim__a_job_due_later_or_of_another_kind__is_not_taken(
    worker_db: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    queue.enqueue(job(kind, run_at=NOW + timedelta(minutes=5)), now=NOW)

    assert queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE) is None
    assert queue.claim(worker_id="w", kinds=[kind + "x"], now=NOW, lease=LEASE) is None
    assert queue.claim(worker_id="w", kinds=[kind], now=NOW + timedelta(minutes=5), lease=LEASE)


@pytest.mark.ac("AC-5.1.2")
async def test_claim__many_workers_at_once__never_take_one_job_twice(
    worker_db: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    for n in range(20):
        queue.enqueue(job(kind, f"job-{n}"), now=NOW)
    claimed: list[uuid.UUID] = []
    guard = threading.Lock()

    def claim_all(worker_id: str) -> None:
        while (
            found := queue.claim(worker_id=worker_id, kinds=[kind], now=NOW, lease=LEASE)
        ) is not None:
            with guard:
                claimed.append(found.id)

    threads = [threading.Thread(target=claim_all, args=(f"w{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 20
    assert len(set(claimed)) == 20


@pytest.mark.ac("AC-5.1.2")
async def test_lock_key__a_second_job_waits_until_the_first_finished(
    worker_db: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    lock = f"shop:{uuid.uuid4()}:etsy"
    first_id = queue.enqueue(job(kind, "first", lock_key=lock), now=NOW)
    queue.enqueue(job(kind, "second", lock_key=lock), now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(seconds=2)

    first = queue.claim(worker_id="w1", kinds=[kind], now=later, lease=LEASE)
    blocked = queue.claim(worker_id="w2", kinds=[kind], now=later, lease=LEASE)
    assert first is not None
    assert first.id == first_id
    assert blocked is None

    queue.complete(first_id, worker_id="w1", now=later)
    second = queue.claim(worker_id="w2", kinds=[kind], now=later, lease=LEASE)
    assert second is not None
    assert second.idempotency_key == "second"


@pytest.mark.ac("AC-5.1.2")
async def test_lock_key__concurrent_claims__at_most_one_running_per_lock(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    locks = [f"shop:{uuid.uuid4()}:etsy" for _ in range(2)]
    for n in range(10):
        queue.enqueue(job(kind, f"job-{n}", lock_key=locks[n % 2]), now=NOW)
    barrier = threading.Barrier(6)
    held: list[ClaimedJob] = []
    guard = threading.Lock()

    def claim_once(worker_id: str) -> None:
        barrier.wait(timeout=10)
        found = queue.claim(worker_id=worker_id, kinds=[kind], now=NOW, lease=LEASE)
        if found is not None:
            with guard:
                held.append(found)

    threads = [threading.Thread(target=claim_once, args=(f"w{n}",)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(j.lock_key or "" for j in held) == sorted(locks)
    with admin.connection() as connection:
        rows = connection.execute(
            """
            select lock_key, count(*) from jobs.jobs
            where kind = %s and state = 'running' group by lock_key
            """,
            [kind],
        ).fetchall()
    assert all(count == 1 for _, count in rows)


# --- Failures, dead letters and leases ------------------------------------------------------


async def test_fail__with_a_retry_plan__is_due_again_at_the_retry_time(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    job_id = queue.enqueue(job(kind), now=NOW)
    queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE)
    retry_at = NOW + timedelta(seconds=7)

    assert queue.fail(
        job_id,
        worker_id="w",
        error="RuntimeError",
        plan=FailurePlan(JobState.RETRY_SCHEDULED, retry_at),
        now=NOW,
    )

    assert state_of(admin, job_id) == ("retry_scheduled", 1, "RuntimeError")
    assert queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE) is None
    again = queue.claim(worker_id="w", kinds=[kind], now=retry_at, lease=LEASE)
    assert again is not None
    assert again.attempts == 2


@pytest.mark.ac("AC-5.1.3")
async def test_dead_letter__visible_to_its_organization_only_with_the_last_error(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    tenant, other = Tenant(admin), Tenant(admin)
    job_id = queue.enqueue(job(kind, organization_id=tenant.organization_id), now=NOW)
    queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE)

    queue.fail(
        job_id,
        worker_id="w",
        error="JobError: shop not connected",
        plan=FailurePlan(JobState.DEAD, None),
        now=NOW,
    )

    [dead] = queue.dead_letters(organization_id=tenant.organization_id, limit=10)
    assert (dead.id, dead.kind, dead.attempts, dead.last_error, dead.failed_at) == (
        job_id,
        kind,
        1,
        "JobError: shop not connected",
        NOW,
    )
    assert queue.dead_letters(organization_id=other.organization_id, limit=10) == []
    assert (
        queue.claim(worker_id="w", kinds=[kind], now=NOW + timedelta(days=1), lease=LEASE) is None
    )


@pytest.mark.ac("AC-5.1.1")
async def test_lease__expired__the_job_is_released_and_the_old_holder_cannot_finish_it(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    job_id = queue.enqueue(job(kind), now=NOW)
    queue.claim(worker_id="crashed", kinds=[kind], now=NOW, lease=LEASE)
    assert queue.extend_lease(job_id, worker_id="crashed", now=NOW, lease=LEASE)
    assert not queue.extend_lease(job_id, worker_id="someone-else", now=NOW, lease=LEASE)

    released = queue.release_expired(now=NOW + LEASE + timedelta(seconds=1))

    assert released >= 1
    state, attempts, error = state_of(admin, job_id)
    assert (state, attempts) == ("retry_scheduled", 1)
    assert error is not None
    assert error.startswith("LeaseExpired")
    assert queue.complete(job_id, worker_id="crashed", now=NOW + LEASE) is False
    retried = queue.claim(worker_id="w2", kinds=[kind], now=NOW + LEASE * 2, lease=LEASE)
    assert retried is not None
    assert retried.attempts == 2
    assert queue.complete(job_id, worker_id="w2", now=NOW + LEASE * 2) is True


async def test_lease__expired_on_the_last_attempt__the_job_is_dead(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    job_id = queue.enqueue(job(kind, retry=RetryPolicy(1, timedelta(seconds=1))), now=NOW)
    queue.claim(worker_id="crashed", kinds=[kind], now=NOW, lease=LEASE)

    queue.release_expired(now=NOW + LEASE * 2)

    assert state_of(admin, job_id)[0] == "dead"


async def test_heartbeats__one_row_per_worker_kept_current(
    worker_db: Database, admin: Database
) -> None:
    beats = PostgresWorkerHeartbeats(worker_db)
    worker_id = f"test-worker-{uuid.uuid4().hex[:8]}"

    beats.beat(
        worker_id=worker_id,
        started_at=NOW,
        now=NOW,
        current_job_id=None,
        release="r1",
        stopping=False,
    )
    beats.beat(
        worker_id=worker_id,
        started_at=NOW,
        now=NOW + timedelta(seconds=20),
        current_job_id=None,
        release="r1",
        stopping=True,
    )

    with admin.connection() as connection:
        row = connection.execute(
            "select last_beat_at, stopping from jobs.worker_heartbeats where worker_id = %s",
            [worker_id],
        ).fetchone()
    assert row == (NOW + timedelta(seconds=20), True)


async def test_prune_finished__keeps_dead_and_recent_jobs(
    worker_db: Database, admin: Database, kind: str
) -> None:
    queue = PostgresJobQueue(worker_db)
    ids = {}
    for key in ("old", "dead"):
        ids[key] = queue.enqueue(job(kind, key), now=NOW)
        queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE)
    queue.complete(ids["old"], worker_id="w", now=NOW - timedelta(days=20))
    queue.fail(
        ids["dead"], worker_id="w", error="E", plan=FailurePlan(JobState.DEAD, None), now=NOW
    )

    queue.prune_finished(before=NOW - timedelta(days=14))

    assert scalar(admin, "select count(*) from jobs.jobs where id = %s", [ids["old"]]) == 0
    assert scalar(admin, "select count(*) from jobs.jobs where id = %s", [ids["dead"]]) == 1


# --- Schedules with two workers ------------------------------------------------------------


@pytest.mark.ac("AC-5.1.6")
async def test_schedule__two_workers_in_one_period__one_job_runs_once(
    worker_db: Database, kind: str
) -> None:
    schedule = Schedule(kind, timedelta(days=1))
    runs: list[str] = []

    async def fan_out(claimed: ClaimedJob) -> None:
        runs.append(claimed.idempotency_key)

    handlers: dict[str, JobHandler] = {kind: fan_out}
    # Early in a period, so the hour below never crosses into the next day (a run just before
    # midnight UTC did, 2026-10-23).
    start = schedule.period_start(NOW) + timedelta(minutes=1)
    clock = [start]
    workers = [
        Worker(
            PostgresJobQueue(worker_db),
            MemoryHeartbeats(),
            handlers,
            config=WorkerConfig(worker_id=f"w{n}"),
            clock=lambda: clock[0],
            jitter=lambda: 0.5,
            tenant=tenant_scope,
            schedules=[schedule],
        )
        for n in range(2)
    ]

    for each in workers:
        await each.run_once()
    clock[0] += timedelta(hours=1)
    for each in workers:
        await each.run_once()

    assert runs == [schedule.key_for(start)]
    assert scalar(worker_db, "select count(*) from jobs.jobs where kind = %s", [kind]) == 1


# --- The worker role: one organization per job ---------------------------------------------


async def test_worker_role__sees_only_the_jobs_organization(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    mine = await connected_shop(admin, ring, tenant)
    theirs = await connected_shop(admin, ring, other)

    def visible() -> set[uuid.UUID]:
        with worker_db.connection() as connection:
            # No tenant filter on purpose: the policies alone decide.
            rows = connection.execute("select shop_id from app.shop_integrations").fetchall()
        return {row[0] for row in rows}

    with tenant_scope(tenant.organization_id):
        assert mine in visible()
        assert theirs not in visible()
    with tenant_scope(None):
        assert visible() == set()


async def test_worker_role__cannot_write_another_organizations_rows(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    theirs = await connected_shop(admin, ring, other)

    with tenant_scope(tenant.organization_id), worker_db.connection() as connection:
        updated = connection.execute(
            "update app.shop_integrations set failure_count = 99 where shop_id = %s returning id",
            [theirs],
        ).fetchall()
        assert updated == []
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute(
                """
                insert into audit.audit_log
                  (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
                values (%s, %s, 'system', 'scheduler', 'test', 'shop', 'x')
                """,
                [uuid.uuid7(), other.organization_id],
            )


@pytest.mark.parametrize(
    "query",
    [
        "select count(*) from private.web_sessions",
        "select count(*) from private.oauth_authorizations",
        "select count(*) from app.memberships",
        "select count(*) from app.organizations",
        "select count(*) from auth.users",
        "delete from audit.audit_log",
    ],
)
async def test_worker_role__has_no_access_to_sessions_identities_or_audit_history(
    worker_db: Database, admin: Database, query: str
) -> None:
    tenant = Tenant(admin)

    with (
        tenant_scope(tenant.organization_id),
        worker_db.connection() as connection,
        pytest.raises(psycopg.Error),
    ):
        connection.execute(query)


async def test_worker_role__reads_and_refreshes_its_shops_tokens_and_stores_probes(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    """What the daily jobs need: the sealed tokens of the job's shop, the probe results, the
    audit record, and `needs_reauth`."""
    tenant = Tenant(admin)
    shop = await connected_shop(admin, ring, tenant)
    vault = PostgresTokenVault(worker_db, EnvelopeSealer(ring))
    target = TenantShop(tenant.organization_id, shop)

    with tenant_scope(tenant.organization_id):
        checks = CapabilityChecks(
            vault=vault,
            marketplace=Probe(),
            store=PostgresCapabilityStore(worker_db),
            tokens=Pinning(),
        )
        probed = await probe_shop_daily(checks, target, now=NOW, correlation_id="job-db")
        loaded = await vault.load(shop)

    assert probed is True
    assert loaded is not None
    assert loaded.tokens is not None
    assert count_capabilities(admin, shop) == len(ShopCapability)
    assert (
        scalar(
            admin,
            "select actor_id from audit.audit_log where entity_id = %s and action = %s",
            [str(shop), "shop.capabilities_checked"],
        )
        == SCHEDULER.id
    )

    with admin.connection() as connection:
        connection.execute(
            "update app.shop_integrations set refresh_expires_at = %s where shop_id = %s",
            [NOW - timedelta(minutes=1), shop],
        )
    with tenant_scope(tenant.organization_id):
        await check_refresh_token(vault, target, now=NOW, correlation_id="job-db")
    assert (
        scalar(admin, "select status from app.shop_integrations where shop_id = %s", [shop])
        == IntegrationStatus.NEEDS_REAUTH.value
    )


# --- Fan-out lists and the purge -----------------------------------------------------------


async def test_shop_directory__connected_and_purgeable_shops_by_id_only(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant = Tenant(admin)
    connected = await connected_shop(admin, ring, tenant)
    leaving = await connected_shop(admin, ring, tenant)
    await PostgresTokenVault(admin, EnvelopeSealer(ring)).disconnect(
        leaving, purge_after=NOW + timedelta(days=30)
    )
    directory = PostgresShopDirectory(worker_db)

    listed = directory.connected_shops()
    due_now = directory.purgeable_shops(now=NOW)
    due_later = directory.purgeable_shops(now=NOW + timedelta(days=31))

    assert TenantShop(tenant.organization_id, connected) in listed
    assert TenantShop(tenant.organization_id, leaving) not in listed
    assert TenantShop(tenant.organization_id, leaving) not in due_now
    assert TenantShop(tenant.organization_id, leaving) in due_later


@pytest.mark.ac("AC-5.1.6")
async def test_purge__deletes_only_the_disconnected_shops_data_and_only_after_its_date(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    leaving = await connected_shop(admin, ring, tenant)
    staying = await connected_shop(admin, ring, tenant)
    elsewhere = await connected_shop(admin, ring, other)
    admin_vault = PostgresTokenVault(admin, EnvelopeSealer(ring))
    for shop in (leaving, elsewhere):
        await admin_vault.disconnect(shop, purge_after=NOW + timedelta(days=30))
    for organization, shop in (
        (tenant.organization_id, leaving),
        (tenant.organization_id, staying),
        (other.organization_id, elsewhere),
    ):
        add_synced_rows(admin, organization, shop)
    store = PostgresPurgeStore(worker_db)
    target = TenantShop(tenant.organization_id, leaving)
    audit = AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=SCHEDULER,
        action="shop.purged",
        entity_type="shop",
        entity_id=str(leaving),
    )

    with tenant_scope(tenant.organization_id):
        early = await store.purge(target, now=NOW + timedelta(days=29), audit=audit)
        assert early is None
        assert count_capabilities(admin, leaving) == 2
        purged = await store.purge(target, now=NOW + timedelta(days=31), audit=audit)
        again = await store.purge(target, now=NOW + timedelta(days=32), audit=audit)

    assert purged == {"shop_capabilities": 2, "etsy_orders": 0, "etsy_order_sync_state": 0}
    assert again is None
    assert count_capabilities(admin, leaving) == 0
    assert count_capabilities(admin, staying) == 2
    assert count_capabilities(admin, elsewhere) == 2
    assert (
        scalar(admin, "select purge_after from app.shop_integrations where shop_id = %s", [leaving])
        is None
    )
    assert scalar(
        admin,
        "select after from audit.audit_log where id = %s",
        [audit.id],
    ) == {"deleted": {"shop_capabilities": 2, "etsy_orders": 0, "etsy_order_sync_state": 0}}


@pytest.mark.ac("AC-5.1.6")
async def test_purge__deletes_the_shops_stored_order_history_with_its_buyer_rows(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    """Post-merge review (P1): the worker's purge deleted the probe results only, then cleared
    `purge_after`, so `private.purge_disconnected_etsy_orders` never matched the shop again and
    the disconnected shop's buyer names and addresses were kept for ever. The purge deletes the
    stored orders (items, refunds and buyer rows with them) and the sync state too."""
    tenant = Tenant(admin)
    leaving = await connected_shop(admin, ring, tenant)
    staying = await connected_shop(admin, ring, tenant)
    admin_vault = PostgresTokenVault(admin, EnvelopeSealer(ring))
    orders = PostgresOrderStore(admin)
    for shop, receipt_id in ((leaving, 51), (staying, 52)):
        integration = await admin_vault.describe(shop)
        assert integration is not None
        await orders.save_backfill_page(integration, [order(receipt_id)], next_offset=None, now=NOW)
    await admin_vault.disconnect(leaving, purge_after=NOW + timedelta(days=30))
    audit = AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=SCHEDULER,
        action="shop.purged",
        entity_type="shop",
        entity_id=str(leaving),
    )

    with tenant_scope(tenant.organization_id):
        purged = await PostgresPurgeStore(worker_db).purge(
            TenantShop(tenant.organization_id, leaving),
            now=NOW + timedelta(days=31),
            audit=audit,
        )

    assert purged == {"shop_capabilities": 0, "etsy_orders": 1, "etsy_order_sync_state": 1}
    assert [count(admin, table, leaving) for table in TABLES] == [0, 0, 0, 0, 0]
    assert [count(admin, table, staying) for table in TABLES] == [1, 1, 1, 1, 1]


async def test_worker_role__never_reads_buyer_rows_or_another_organizations_orders(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    """The purge's grants are the least it needs: the worker reads no buyer row, and it deletes
    no order of another organization."""
    tenant, other = Tenant(admin), Tenant(admin)
    theirs = await connected_shop(admin, ring, other)
    integration = await PostgresTokenVault(admin, EnvelopeSealer(ring)).describe(theirs)
    assert integration is not None
    await PostgresOrderStore(admin).save_backfill_page(
        integration, [order(53)], next_offset=None, now=NOW
    )

    with tenant_scope(tenant.organization_id), worker_db.connection() as connection:
        deleted = connection.execute(
            "delete from app.etsy_orders where shop_id = %s", [theirs]
        ).rowcount
        assert deleted == 0
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("select buyer_name from app.etsy_order_buyers").fetchall()
    with (
        tenant_scope(other.organization_id),
        worker_db.connection() as connection,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        connection.execute("select buyer_name from app.etsy_order_buyers").fetchall()
    assert count(admin, "etsy_orders", theirs) == 1


async def test_purge__in_another_organizations_scope__finds_nothing_to_purge(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant, other = Tenant(admin), Tenant(admin)
    leaving = await connected_shop(admin, ring, tenant)
    await PostgresTokenVault(admin, EnvelopeSealer(ring)).disconnect(
        leaving, purge_after=NOW + timedelta(days=30)
    )
    add_synced_rows(admin, tenant.organization_id, leaving)
    audit = AuditRecord.new(
        organization_id=other.organization_id,
        actor=SCHEDULER,
        action="shop.purged",
        entity_type="shop",
        entity_id=str(leaving),
    )

    with tenant_scope(other.organization_id):
        purged = await PostgresPurgeStore(worker_db).purge(
            TenantShop(tenant.organization_id, leaving),
            now=NOW + timedelta(days=31),
            audit=audit,
        )

    assert purged is None
    assert count_capabilities(admin, leaving) == 2


async def test_reconnect_before_the_purge__keeps_the_shops_data(
    worker_db: Database, admin: Database, ring: KeyRing
) -> None:
    tenant = Tenant(admin)
    connector = PostgresShopConnector(admin, PostgresTokenVault(admin, EnvelopeSealer(ring)))
    etsy = etsy_ids()
    shop = (await connector.connect(request_for(tenant, etsy))).shop.id
    await PostgresTokenVault(admin, EnvelopeSealer(ring)).disconnect(
        shop, purge_after=NOW + timedelta(days=30)
    )
    await connector.connect(request_for(tenant, etsy))
    add_synced_rows(admin, tenant.organization_id, shop)
    audit = AuditRecord.new(
        organization_id=tenant.organization_id,
        actor=SCHEDULER,
        action="shop.purged",
        entity_type="shop",
        entity_id=str(shop),
    )

    with tenant_scope(tenant.organization_id):
        purged = await PostgresPurgeStore(worker_db).purge(
            TenantShop(tenant.organization_id, shop), now=NOW + timedelta(days=31), audit=audit
        )

    assert purged is None
    assert count_capabilities(admin, shop) == 2


async def test_claim__returns_the_jobs_correlation_and_causation(
    worker_db: Database, kind: str
) -> None:
    """ADR-0019: the IDs travel from the trigger through the job."""
    queue = PostgresJobQueue(worker_db)
    queue.enqueue(job(kind, correlation_id="req-9", causation_id="execution:9"), now=NOW)

    claimed = queue.claim(worker_id="w", kinds=[kind], now=NOW, lease=LEASE)

    assert claimed is not None
    assert (claimed.correlation_id, claimed.causation_id) == ("req-9", "execution:9")
