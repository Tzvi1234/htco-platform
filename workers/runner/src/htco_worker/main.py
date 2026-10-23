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
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo

import anyio
import httpx
import structlog

from htco_application.approvals import GatewayApprovals, MemoryApprovalService
from htco_application.automation.durable import (
    EXECUTION_JOB,
    DurableAutomationEngine,
    Handler,
)
from htco_application.automation.records import registry_from_rows
from htco_application.etsy_tokens import VaultAccessTokenProvider
from htco_application.etsy_webhooks import (
    ORDER_INTAKE,
    WEBHOOK_JOB,
    EngineOrderIntake,
    WebhookProcessor,
)
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import (
    ActionGateway,
    Channel,
    UnavailableGateway,
)
from htco_application.execution.guards import Guards
from htco_application.execution.service import (
    EVENT_DESTINATION,
    ActionGatewayService,
    GatewayParts,
)
from htco_application.jobs import ClaimedJob, JobError, JobQueue
from htco_application.maintenance import (
    EXPIRY_JOB,
    EXPIRY_SCHEDULE,
    PROBE_JOB,
    PROBE_SCHEDULE,
    PRUNE_SCHEDULE,
    PURGE_JOB,
    PURGE_SCHEDULE,
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
    PendingDelivery,
    deliver_outbox_message,
)
from htco_application.order_intake import (
    ORDER_SYNC_JOB,
    RECONCILIATION_TICK,
    EngineSyncTrigger,
    OrderIntake,
    due_reconciliations,
    enqueue_reconciliations,
)
from htco_application.shop_capabilities import CapabilityChecks
from htco_application.workforce.files import read_action_catalog, read_registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.guards import BreakerPolicy
from htco_domain.integrations import TokenHealth
from htco_integrations.etsy import (
    EtsyAdapter,
    EtsyConfig,
    EtsyCredentials,
    app_key_fingerprint,
)
from htco_integrations.etsy_oauth import EtsyOAuthClient
from htco_persistence.actions import PostgresActionStore
from htco_persistence.automation import PostgresAutomationCatalog, PostgresExecutionStore
from htco_persistence.database import Database, tenant_scope
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.etsy_webhooks import PostgresWebhookInbox, PostgresWorkerShops
from htco_persistence.guards import PostgresBreakers, PostgresRateLimiter, PostgresSlots
from htco_persistence.jobs import PostgresJobQueue, PostgresWorkerHeartbeats
from htco_persistence.maintenance import PostgresPurgeStore, PostgresShopDirectory
from htco_persistence.messaging import PostgresOutbox
from htco_persistence.shop_integrations import (
    PostgresCapabilityStore,
    PostgresRefreshLock,
    PostgresTokenVault,
)
from htco_persistence.worker_gateway import PostgresShopFacts
from htco_platform.crypto import EnvelopeSealer
from htco_platform.http import new_async_http_client
from htco_platform.logs import configure_logging
from htco_platform.settings import ConfigurationError, load_settings
from htco_platform.tracing import new_tracer_provider
from htco_worker.health import serve_health
from htco_worker.runner import JobHandler, Worker, WorkerConfig
from htco_worker.settings import SCHEDULES, WorkerSettings

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


class AcceptedEvents:
    """`events.publish` (slice 5.12): events such as `actions.executed.v1` stay in the outbox as
    the record; their consumers subscribe from Phase 6, so delivering is accepting them."""

    async def deliver(self, delivery: PendingDelivery) -> None:
        _log.info(
            "event.accepted",
            event_type=delivery.message.payload.get("event_type"),
            outbox_id=str(delivery.id),
        )


def outbox_handlers(
    store: OutboxStore, deliverers: Mapping[str, Deliverer]
) -> dict[str, JobHandler]:
    """The `outbox.deliver` job (slice 5.2). The pilot has no destination yet (HTCO writes
    nothing to Etsy; email arrives in Phase 6), so a message would fail into the dead-letter
    state; nothing writes one before a destination exists."""

    async def deliver(job: ClaimedJob) -> None:
        await deliver_outbox_message(store, deliverers, job, now=_now())

    return {OUTBOX_DELIVERY_JOB: deliver}


def automation_handlers(engine: DurableAutomationEngine, worker_id: str) -> dict[str, JobHandler]:
    """The `automation.execute` job (slice 5.10): one run of a durable execution."""
    actor = ActorRef(ActorKind.SYSTEM, f"agent-worker:{worker_id}"[:200])

    async def execute(job: ClaimedJob) -> None:
        raw = job.payload.get("execution_id")
        if not isinstance(raw, str):
            raise JobError("an execution job names its execution")
        execution_id = uuid.UUID(raw)
        await engine.execute(execution_id, worker=actor)

    return {EXECUTION_JOB: execute}


def worker_engine(
    database: Database,
    *,
    gateway: ActionGateway | None = None,
    handlers: Mapping[str, Handler] | None = None,
) -> DurableAutomationEngine:
    """The engine on the worker's pool: definitions from the database, the deployed handlers
    (Etsy Order Intake, slice 5.4) and the Action Gateway. A deployed handler still runs only once
    its automation is `ready` (the owner's activation, slice 5.11)."""
    catalog = PostgresAutomationCatalog(database)
    return DurableAutomationEngine(
        lambda: registry_from_rows(catalog.current()),
        PostgresExecutionStore(database),
        dict(handlers or {}),
        clock=_now,
        jitter=random.random,
        gateway=gateway or UnavailableGateway(),
        offload=anyio.to_thread.run_sync,
    )


class NoUsers:
    """Users never act in the worker: a user actor is refused as unknown."""

    def principal(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> None:  # noqa: ARG002 - the port
        return None

    def organization_of_shop(self, shop_id: uuid.UUID) -> None:  # noqa: ARG002 - the port
        return None


def worker_gateway(
    settings: WorkerSettings, database: Database, adapter: EtsyAdapter | None
) -> ActionGateway:
    """The real Action Gateway for reads (`HTCO_WORKER_ACTION_GATEWAY=reads`, the default), or
    `UnavailableGateway` (`off`, or without the Etsy app). Writes never run: they wait for an
    approval that only Phase 7's approval store can give."""
    if settings.worker_action_gateway == "off" or adapter is None:
        return UnavailableGateway()
    tools = read_registry(settings.registry_path).registry()
    catalog = PostgresAutomationCatalog(database)
    facts = PostgresShopFacts(database)
    users = NoUsers()
    return ActionGatewayService(
        GatewayParts(
            catalog=read_action_catalog(
                settings.registry_path, settings.action_schemas_path, tools
            ),
            registry=lambda: registry_from_rows(catalog.current()),
            store=PostgresActionStore(database),
            principals=users,
            shops=users,
            # The shared approval contract (5.14) in memory: a write waits for an approval
            # no one can give in the worker (Postgres approvals are Phase 7), so none runs.
            approvals=GatewayApprovals(MemoryApprovalService(clock=_now), clock=_now),
            adapters={Channel.API: EtsyApiChannel(adapter, facts)},
            capabilities=facts,
            clock=_now,
            guards=Guards(
                breakers=PostgresBreakers(database, BreakerPolicy()),
                rates=PostgresRateLimiter(database),
                slots=PostgresSlots(database),
            ),
        )
    )


def intake_handlers(database: Database, vault: PostgresTokenVault) -> dict[str, Handler]:
    """Etsy Order Intake's handler (slice 5.4)."""
    return {ORDER_INTAKE: OrderIntake(store=PostgresOrderStore(database), shops=vault, clock=_now)}


def reconciliation_handlers(  # noqa: PLR0913 - the tick and its collaborators
    enabled: tuple[str, ...],
    *,
    queue: JobQueue,
    directory: ShopDirectory,
    times: tuple[time, ...],
    tz: tzinfo,
    clock: Callable[[], datetime] = _now,
) -> dict[str, JobHandler]:
    """The hourly reconciliation tick (slice 5.5), when named: in the hour of a configured time
    it enqueues one `etsy.order_sync` job per connected shop (the sweep for the day's last time,
    a reconcile otherwise), which `order_sync_handlers` runs."""
    if RECONCILIATION_TICK.kind not in enabled:
        return {}

    def tick() -> int:
        now = clock()
        return sum(
            enqueue_reconciliations(
                directory, queue, reason=run.reason, slot=run.slot, now=now, run_at=run.at
            )
            for run in due_reconciliations(times, now=now, tz=tz)
        )

    return {RECONCILIATION_TICK.kind: _in_thread(tick, "orders.reconciliations_enqueued")}


def order_sync_handlers(engine: DurableAutomationEngine) -> dict[str, JobHandler]:
    """The `etsy.order_sync` job (slice 5.4): a requested history run of the shop's orders."""
    trigger = EngineSyncTrigger(engine, offload=anyio.to_thread.run_sync)

    async def run(job: ClaimedJob) -> None:
        started = await trigger.run(job)
        _log.info("etsy.order_sync_requested", started=started, job_id=str(job.id))

    return {ORDER_SYNC_JOB: run}


def webhook_handlers(processor: WebhookProcessor) -> dict[str, JobHandler]:
    """The `etsy.webhook_event` job (slice 5.3): re-read what the webhook names."""

    async def process(job: ClaimedJob) -> None:
        outcome = await processor.process(job)
        _log.info("etsy.webhook_processed", outcome=outcome, job_id=str(job.id))

    return {WEBHOOK_JOB: process}


def webhook_processor(
    database: Database, engine: DurableAutomationEngine, adapter: EtsyAdapter
) -> WebhookProcessor:
    inbox = PostgresWebhookInbox(database)
    return WebhookProcessor(
        orders=EngineOrderIntake(engine, offload=anyio.to_thread.run_sync),
        catalog=adapter,
        shops=PostgresWorkerShops(database),
        outbox=PostgresOutbox(database),
        marks=inbox,
        clock=_now,
        offload=anyio.to_thread.run_sync,
    )


def gateway_etsy_config(*, daily_quota_floor: int) -> EtsyConfig:
    """The adapter behind the gateway's API channel fails fast: one attempt, and a 429 is raised
    at once with its `Retry-After`. The gateway's guards (5.15) own retries, inline waits and
    defers, so one read makes at most `InlineRetry.max_attempts` calls (audit P1-2)."""
    return EtsyConfig(
        max_attempts=1, max_retry_after_seconds=0, daily_quota_floor=daily_quota_floor
    )


@dataclass(frozen=True)
class EtsyParts:
    vault: PostgresTokenVault | None = None
    checks: CapabilityChecks | None = None
    # With its own retries, for the capability probes and the webhook re-reads.
    adapter: EtsyAdapter | None = None
    # Failing fast, for the Action Gateway's API channel.
    gateway_adapter: EtsyAdapter | None = None


def etsy_parts(settings: WorkerSettings, database: Database, http: httpx.AsyncClient) -> EtsyParts:
    ring = settings.key_ring
    if ring is None:
        return EtsyParts()
    # Like the API: tokens issued for a replaced Etsy app read as `needs_reauth`.
    app_key = (
        None
        if settings.etsy_keystring is None
        else app_key_fingerprint(settings.etsy_keystring.get_secret_value())
    )
    vault = PostgresTokenVault(database, EnvelopeSealer(ring), app_key_fingerprint=app_key)
    if settings.etsy_keystring is None or settings.etsy_shared_secret is None:
        return EtsyParts(vault=vault)
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
    gateway_adapter = EtsyAdapter(
        http,
        credentials=credentials,
        access_tokens=tokens,
        config=gateway_etsy_config(daily_quota_floor=settings.etsy_daily_quota_floor),
    )
    return EtsyParts(vault, checks, adapter, gateway_adapter)


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
    etsy = etsy_parts(settings, database, http)
    vault, checks, adapter = etsy.vault, etsy.checks, etsy.adapter
    handlers = maintenance_handlers(
        settings.schedules,
        queue=queue,
        directory=PostgresShopDirectory(database),
        vault=vault,
        checks=checks,
        purge=PostgresPurgeStore(database),
    ) | outbox_handlers(PostgresOutbox(database), {EVENT_DESTINATION: AcceptedEvents()})
    gateway = worker_gateway(settings, database, etsy.gateway_adapter)
    deployed = {} if vault is None or adapter is None else intake_handlers(database, vault)
    engine = worker_engine(database, gateway=gateway, handlers=deployed)
    handlers |= automation_handlers(engine, settings.worker_id)
    if adapter is not None:  # webhook and sync jobs wait in the queue until Etsy is configured
        handlers |= webhook_handlers(webhook_processor(database, engine, adapter))
        handlers |= order_sync_handlers(engine)
        handlers |= reconciliation_handlers(
            settings.schedules,
            queue=queue,
            directory=PostgresShopDirectory(database),
            times=settings.reconciliation_times,
            tz=settings.reconciliation_zone,
        )
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
