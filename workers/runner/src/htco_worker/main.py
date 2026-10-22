"""The `agent-worker` process and its composition root: `python -m htco_worker`.

It reads its settings (exit 78 when they are wrong), opens a database pool that runs as the
`htco_worker` role, wires the job handlers of the enabled maintenance schedules, serves the
health port and runs the worker loop until SIGTERM or SIGINT. Etsy is reached only through the
guarded, read-only `EtsyAdapter` (ADR-0008): the probes are GETs, and the adapter refuses any
other method towards the shop API.
"""

import os
import random
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

import anyio
import httpx
import structlog

from htco_application.etsy_tokens import VaultAccessTokenProvider
from htco_application.jobs import ClaimedJob, JobQueue
from htco_application.maintenance import (
    EXPIRY_JOB,
    EXPIRY_SCHEDULE,
    PROBE_JOB,
    PROBE_SCHEDULE,
    PRUNE_SCHEDULE,
    PURGE_JOB,
    PURGE_SCHEDULE,
    SCHEDULES,
    ExpiryVault,
    PurgeStore,
    ShopDirectory,
    TenantShop,
    check_refresh_token,
    enqueue_capability_probes,
    enqueue_purges,
    enqueue_refresh_token_checks,
    probe_shop_daily,
    prune_finished_jobs,
    purge_disconnected_shop,
)
from htco_application.messaging import (
    OUTBOX_DELIVERY_JOB,
    Deliverer,
    OutboxStore,
    deliver_outbox_message,
)
from htco_application.shop_capabilities import CapabilityChecks
from htco_domain.integrations import TokenHealth
from htco_integrations.etsy import EtsyAdapter, EtsyConfig, EtsyCredentials
from htco_integrations.etsy_oauth import EtsyOAuthClient
from htco_persistence.database import Database, tenant_scope
from htco_persistence.jobs import PostgresJobQueue, PostgresWorkerHeartbeats
from htco_persistence.maintenance import PostgresPurgeStore, PostgresShopDirectory
from htco_persistence.messaging import PostgresOutbox
from htco_persistence.shop_integrations import (
    PostgresCapabilityStore,
    PostgresRefreshLock,
    PostgresTokenVault,
)
from htco_platform.crypto import EnvelopeSealer
from htco_platform.http import new_async_http_client
from htco_platform.logs import configure_logging
from htco_platform.settings import ConfigurationError, load_settings
from htco_platform.tracing import new_tracer_provider
from htco_worker.health import serve_health
from htco_worker.runner import JobHandler, Worker, WorkerConfig
from htco_worker.settings import WorkerSettings

EXIT_CONFIGURATION_ERROR = 78
# sysexits.h EX_TEMPFAIL: stopped before the current job finished; its lease brings it back.
EXIT_TEMPORARY_FAILURE = 75
WORKER_ROLE = "htco_worker"

_log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _shop(job: ClaimedJob) -> TenantShop:
    if job.organization_id is None or job.shop_id is None:
        raise ValueError(f"{job.kind} needs a shop and its organization")
    return TenantShop(organization_id=job.organization_id, shop_id=job.shop_id)


def _in_thread(work: Callable[[], int], event: str) -> JobHandler:
    async def handle(job: ClaimedJob) -> None:
        count = await anyio.to_thread.run_sync(work)
        _log.info(event, count=count, job_id=str(job.id))

    return handle


def maintenance_handlers(  # noqa: PLR0913 - the handlers' collaborators
    enabled: tuple[str, ...],
    *,
    queue: JobQueue,
    directory: ShopDirectory,
    vault: ExpiryVault | None,
    checks: CapabilityChecks | None,
    purge: PurgeStore,
) -> dict[str, JobHandler]:
    """The job handlers of the enabled schedules: each schedule's fan-out and its per-shop job."""
    handlers: dict[str, JobHandler] = {}
    if PROBE_SCHEDULE.kind in enabled and checks is not None:
        probes = checks
        handlers[PROBE_SCHEDULE.kind] = _in_thread(
            lambda: enqueue_capability_probes(directory, queue, now=_now()),
            "maintenance.probes_enqueued",
        )

        async def probe(job: ClaimedJob) -> None:
            await probe_shop_daily(probes, _shop(job), now=_now(), correlation_id=str(job.id))

        handlers[PROBE_JOB] = probe
    if EXPIRY_SCHEDULE.kind in enabled and vault is not None:
        tokens = vault
        handlers[EXPIRY_SCHEDULE.kind] = _in_thread(
            lambda: enqueue_refresh_token_checks(directory, queue, now=_now()),
            "maintenance.expiry_checks_enqueued",
        )

        async def expiry(job: ClaimedJob) -> None:
            shop = _shop(job)
            health = await check_refresh_token(tokens, shop, now=_now(), correlation_id=str(job.id))
            if health in {TokenHealth.EXPIRING, TokenHealth.NEEDS_REAUTH}:
                _log.warning(
                    "etsy.refresh_token_expiring", shop_id=str(shop.shop_id), health=health
                )

        handlers[EXPIRY_JOB] = expiry
    if PURGE_SCHEDULE.kind in enabled:
        handlers[PURGE_SCHEDULE.kind] = _in_thread(
            lambda: enqueue_purges(directory, queue, now=_now()), "maintenance.purges_enqueued"
        )

        async def purge_shop(job: ClaimedJob) -> None:
            deleted = await purge_disconnected_shop(
                purge, _shop(job), now=_now(), correlation_id=str(job.id)
            )
            _log.info("shop.purged", shop_id=str(job.shop_id), deleted=deleted)

        handlers[PURGE_JOB] = purge_shop
    if PRUNE_SCHEDULE.kind in enabled:
        handlers[PRUNE_SCHEDULE.kind] = _in_thread(
            lambda: prune_finished_jobs(queue, now=_now()), "maintenance.jobs_pruned"
        )
    return handlers


def outbox_handlers(
    store: OutboxStore, deliverers: Mapping[str, Deliverer]
) -> dict[str, JobHandler]:
    """The `outbox.deliver` job (slice 5.2). The pilot has no destination yet (HTCO writes
    nothing to Etsy; email arrives in Phase 6), so a message would fail into the dead-letter
    state; nothing writes one before a destination exists."""

    async def deliver(job: ClaimedJob) -> None:
        await deliver_outbox_message(store, deliverers, job, now=_now())

    return {OUTBOX_DELIVERY_JOB: deliver}


def _etsy_checks(
    settings: WorkerSettings, database: Database, http: httpx.AsyncClient
) -> tuple[PostgresTokenVault | None, CapabilityChecks | None]:
    ring = settings.key_ring
    if ring is None:
        return None, None
    vault = PostgresTokenVault(database, EnvelopeSealer(ring))
    if settings.etsy_keystring is None or settings.etsy_shared_secret is None:
        return vault, None
    credentials = EtsyCredentials(
        keystring=settings.etsy_keystring.get_secret_value(),
        shared_secret=settings.etsy_shared_secret.get_secret_value(),
    )
    tokens = VaultAccessTokenProvider(
        vault,
        EtsyOAuthClient(http, credentials=credentials),
        PostgresRefreshLock(database),
        clock=_now,
    )
    adapter = EtsyAdapter(
        http,
        credentials=credentials,
        access_tokens=tokens,
        config=EtsyConfig(daily_quota_floor=settings.etsy_daily_quota_floor),
    )
    checks = CapabilityChecks(
        vault=vault,
        marketplace=adapter,
        store=PostgresCapabilityStore(database),
        tokens=tokens,
        webhooks_configured=False,
    )
    return vault, checks


async def _serve(settings: WorkerSettings) -> None:
    database = Database(
        settings.database_url.get_secret_value(), min_size=1, max_size=4, role=WORKER_ROLE
    )
    http = new_async_http_client(
        tracer_provider=new_tracer_provider(
            service_name="htco-worker", environment=settings.environment
        )
    )
    queue = PostgresJobQueue(database)
    vault, checks = _etsy_checks(settings, database, http)
    handlers = maintenance_handlers(
        settings.schedules,
        queue=queue,
        directory=PostgresShopDirectory(database),
        vault=vault,
        checks=checks,
        purge=PostgresPurgeStore(database),
    ) | outbox_handlers(PostgresOutbox(database), {})
    worker = Worker(
        queue,
        PostgresWorkerHeartbeats(database),
        handlers,
        config=WorkerConfig(
            worker_id=settings.worker_id,
            lease=timedelta(seconds=settings.worker_lease_seconds),
            poll_interval=settings.worker_poll_seconds,
            shutdown_grace=settings.worker_shutdown_grace_seconds,
            release=settings.release,
        ),
        clock=_now,
        jitter=random.random,
        tenant=tenant_scope,
        schedules=[SCHEDULES[name] for name in settings.schedules],
        # Blocking work past the grace period: leave now; the job's lease brings it back.
        on_overrun=lambda: os._exit(EXIT_TEMPORARY_FAILURE),
    )
    await database.start()
    stop = anyio.Event()
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(stop_on_signal, stop)
            if settings.worker_health_port:
                group.start_soon(
                    lambda: serve_health(
                        settings.worker_health_port, alive=lambda: worker.is_alive(_now())
                    )
                )
            await worker.run(stop)
            group.cancel_scope.cancel()
    finally:
        await http.aclose()
        await database.stop()


async def stop_on_signal(stop: anyio.Event) -> None:
    try:
        with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
            async for received in signals:
                _log.info("worker.signal", signal=signal.Signals(received).name)
                stop.set()
                return
    except NotImplementedError:  # Windows: Ctrl+C ends the process instead
        await anyio.sleep_forever()


def main(*, serve: Callable[[WorkerSettings], Awaitable[None]] = _serve) -> int:
    try:
        settings = load_settings(WorkerSettings)
    except ConfigurationError as error:
        sys.stderr.write(f"htco-worker: invalid configuration\n{error}\n")
        return EXIT_CONFIGURATION_ERROR
    configure_logging(level=settings.log_level)
    _log.info(
        "worker.starting",
        environment=settings.environment,
        release=settings.release,
        worker_id=settings.worker_id,
        schedules=list(settings.schedules),
        run_id=str(uuid.uuid7()),
    )
    anyio.run(serve, settings)
    return 0
