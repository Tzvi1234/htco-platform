"""Scheduled maintenance (slice 5.1): daily capability probes, refresh-token expiry checks, and
the purge of disconnected shops' data after their purge date.

Each maintenance schedule runs one job per day that only lists the shops concerned (IDs only,
across organizations) and enqueues one job per shop. The per-shop job then runs inside that
shop's organization, so the worker's database role reaches nothing else (row-level security on
the organization). Every per-shop job is idempotent and keyed by the day, so a second worker or
a replayed schedule changes nothing (AC-5.1.6).

Etsy is only read: the probes are the GET-only probes of slice 4.5 through the guarded adapter;
the expiry check reads the stored expiry date and never calls Etsy.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.audit import AuditRecord
from htco_application.jobs import JobQueue, JobSpec
from htco_application.shop_capabilities import (
    CapabilityChecks,
    ShopNotConnectedError,
    probe_capabilities,
)
from htco_application.shop_integrations import ShopIntegration
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import IntegrationStatus, TokenHealth, token_health
from htco_domain.jobs import Schedule

DAILY = timedelta(days=1)
PROBE_SCHEDULE = Schedule("maintenance.capability_probes", DAILY)
EXPIRY_SCHEDULE = Schedule("maintenance.refresh_token_expiry", DAILY)
PURGE_SCHEDULE = Schedule("maintenance.purge_disconnected_shops", DAILY)
PRUNE_SCHEDULE = Schedule("maintenance.prune_finished_jobs", DAILY)
SCHEDULES = {s.kind: s for s in (PROBE_SCHEDULE, EXPIRY_SCHEDULE, PURGE_SCHEDULE, PRUNE_SCHEDULE)}
# Succeeded jobs are kept this long (their keys deduplicate replays meanwhile); dead ones stay.
FINISHED_JOB_RETENTION = timedelta(days=14)

PROBE_JOB = "etsy.capability_probe"
EXPIRY_JOB = "etsy.refresh_token_check"
PURGE_JOB = "shops.purge"

SCHEDULER = ActorRef(ActorKind.SYSTEM, "scheduler")


@dataclass(frozen=True)
class TenantShop:
    organization_id: uuid.UUID
    shop_id: uuid.UUID


class ShopDirectory(Protocol):
    """Shops across organizations, by ID only: what the daily fan-out needs, nothing more."""

    def connected_shops(self) -> list[TenantShop]: ...

    def purgeable_shops(self, *, now: datetime) -> list[TenantShop]: ...


class ExpiryVault(Protocol):
    """The part of the token vault the expiry check uses: it never opens a token."""

    async def describe(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...

    async def mark_needs_reauth(
        self,
        shop_id: uuid.UUID,
        *,
        audit: AuditRecord | None = None,
        generation: int | None = None,
    ) -> bool: ...


class PurgeStore(Protocol):
    async def purge(
        self, shop: TenantShop, *, now: datetime, audit: AuditRecord
    ) -> dict[str, int] | None:
        """In one transaction, and only while the shop's integration is still disconnected, its
        purge date has passed and it was not purged yet: delete the shop's synced data (never
        another shop's), mark it purged and append `audit` with the deleted rows per table.
        Returns those counts, or None when nothing was due."""
        ...


# --- The daily fan-out (system jobs) -------------------------------------------------------


def _fan_out(kind: str, shops: Sequence[TenantShop], queue: JobQueue, *, now: datetime) -> int:
    day = now.strftime("%Y-%m-%d")
    for shop in shops:
        queue.enqueue(
            JobSpec(
                kind=kind,
                idempotency_key=f"{kind}:{shop.shop_id}:{day}",
                organization_id=shop.organization_id,
                shop_id=shop.shop_id,
                # One Etsy-facing job per shop at a time.
                lock_key=f"shop:{shop.shop_id}:etsy",
            ),
            now=now,
        )
    return len(shops)


def enqueue_capability_probes(directory: ShopDirectory, queue: JobQueue, *, now: datetime) -> int:
    return _fan_out(PROBE_JOB, directory.connected_shops(), queue, now=now)


def enqueue_refresh_token_checks(
    directory: ShopDirectory, queue: JobQueue, *, now: datetime
) -> int:
    return _fan_out(EXPIRY_JOB, directory.connected_shops(), queue, now=now)


def enqueue_purges(directory: ShopDirectory, queue: JobQueue, *, now: datetime) -> int:
    return _fan_out(PURGE_JOB, directory.purgeable_shops(now=now), queue, now=now)


def prune_finished_jobs(queue: JobQueue, *, now: datetime) -> int:
    return queue.prune_finished(before=now - FINISHED_JOB_RETENTION)


# --- Per-shop jobs (run inside the shop's organization) ---------------------------------------


async def probe_shop_daily(
    checks: CapabilityChecks, shop: TenantShop, *, now: datetime, correlation_id: str
) -> bool:
    """The daily read-only probe of one shop. False: skipped (no longer connected, or a
    disconnect or reconnect overtook the probe), which is not a failure."""
    integration = await checks.vault.load(shop.shop_id)
    if integration is None or integration.organization_id != shop.organization_id:
        return False
    try:
        await probe_capabilities(
            checks,
            integration,
            now=now,
            actor=SCHEDULER,
            trigger="daily",
            correlation_id=correlation_id,
        )
    except ShopNotConnectedError:  # includes ProbeSupersededError
        return False
    return True


async def check_refresh_token(
    vault: ExpiryVault, shop: TenantShop, *, now: datetime, correlation_id: str
) -> TokenHealth | None:
    """Read the stored refresh-token expiry (no Etsy call). An expired token moves a connected
    shop to `needs_reauth` (audited), so no call is attempted with it; `EXPIRING` is returned
    for the worker to warn about. None: the shop has no integration in this organization."""
    integration = await vault.describe(shop.shop_id)
    if integration is None or integration.organization_id != shop.organization_id:
        return None
    health = token_health(integration.status, integration.refresh_expires_at, now=now)
    if health is TokenHealth.NEEDS_REAUTH and integration.status is IntegrationStatus.CONNECTED:
        await vault.mark_needs_reauth(
            shop.shop_id,
            generation=integration.generation,
            audit=AuditRecord.new(
                organization_id=shop.organization_id,
                actor=SCHEDULER,
                action="shop.needs_reauth",
                entity_type="shop",
                entity_id=str(shop.shop_id),
                after={"reason": "refresh_token_expired"},
                correlation_id=correlation_id,
            ),
        )
    return health


async def purge_disconnected_shop(
    store: PurgeStore, shop: TenantShop, *, now: datetime, correlation_id: str
) -> dict[str, int] | None:
    """Delete one disconnected shop's synced data once its purge date has passed (FR-SHOP-04,
    DATA_MODEL §6). The store re-checks the dates in the same transaction, so a reconnect that
    happened after the shop was listed keeps its data."""
    return await store.purge(
        shop,
        now=now,
        audit=AuditRecord.new(
            organization_id=shop.organization_id,
            actor=SCHEDULER,
            action="shop.purged",
            entity_type="shop",
            entity_id=str(shop.shop_id),
            correlation_id=correlation_id,
        ),
    )
