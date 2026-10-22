"""Scheduled maintenance (slice 5.1): the daily fan-out and the per-shop jobs, with fakes.

The fan-out enqueues one job per shop and day, so running it twice (two workers, or a replayed
schedule) adds nothing (AC-5.1.6). The probes only read (the fake marketplace records every
call); the expiry check never calls Etsy; the purge goes through a store that re-checks the
purge date in its own transaction (proven against Postgres in tests/integration).
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_application.maintenance import (
    EXPIRY_JOB,
    PROBE_JOB,
    PURGE_JOB,
    SCHEDULER,
    TenantShop,
    check_refresh_token,
    enqueue_capability_probes,
    enqueue_purges,
    enqueue_refresh_token_checks,
    probe_shop_daily,
    prune_finished_jobs,
    purge_disconnected_shop,
)
from htco_application.shop_capabilities import CapabilityChecks
from htco_application.shop_integrations import EtsyConnection, OAuthTokens
from htco_domain.integrations import IntegrationStatus, TokenHealth
from htco_domain.jobs import JobState
from tests.etsy_fakes import ETSY_SHOP, NOW, Capabilities, Pinning, Probe, Vault
from tests.job_fakes import MemoryJobQueue, a_job

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("c0000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("d0000000-0000-0000-0000-000000000000")
SHOP = TenantShop(ORG, uuid.UUID("c3000000-0000-0000-0000-000000000001"))
SECOND = TenantShop(OTHER_ORG, uuid.UUID("d3000000-0000-0000-0000-000000000001"))
SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})


@dataclass
class Directory:
    connected: list[TenantShop] = field(default_factory=list)
    purgeable: list[TenantShop] = field(default_factory=list)
    asked_at: list[datetime] = field(default_factory=list)

    def connected_shops(self) -> list[TenantShop]:
        return list(self.connected)

    def purgeable_shops(self, *, now: datetime) -> list[TenantShop]:
        self.asked_at.append(now)
        return list(self.purgeable)


@dataclass
class Purges:
    due: set[uuid.UUID] = field(default_factory=set)
    audits: list[AuditRecord] = field(default_factory=list)

    async def purge(
        self,
        shop: TenantShop,
        *,
        now: datetime,  # noqa: ARG002 - the port's signature
        audit: AuditRecord,
    ) -> dict[str, int] | None:
        if shop.shop_id not in self.due:
            return None
        self.due.discard(shop.shop_id)
        self.audits.append(audit)
        return {"shop_capabilities": 7}


def vault_with(shop: TenantShop, *, refresh_left: timedelta = timedelta(days=90)) -> Vault:
    vault = Vault()
    vault.add(
        EtsyConnection(
            organization_id=shop.organization_id,
            shop_id=shop.shop_id,
            etsy_shop_id=ETSY_SHOP,
            etsy_user_id=12345678,
            scopes=SCOPES,
            tokens=OAuthTokens(
                access_token="12345678.fake-access-token",  # noqa: S106 - fake
                refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                access_expires_at=NOW + timedelta(hours=1),
                refresh_expires_at=NOW + refresh_left,
            ),
        )
    )
    return vault


def checks_for(vault: Vault, probe: Probe | None = None) -> CapabilityChecks:
    return CapabilityChecks(
        vault=vault, marketplace=probe or Probe(), store=Capabilities(), tokens=Pinning()
    )


# --- fan-out ------------------------------------------------------------------------------


@pytest.mark.ac("AC-5.1.6")
def test_enqueue_capability_probes__twice_on_one_day__one_job_per_shop() -> None:
    queue = MemoryJobQueue()
    directory = Directory(connected=[SHOP, SECOND])

    first = enqueue_capability_probes(directory, queue, now=NOW)
    enqueue_capability_probes(directory, queue, now=NOW + timedelta(hours=5))

    jobs = queue.of_kind(PROBE_JOB)
    assert first == 2
    assert {(j.spec.organization_id, j.spec.shop_id) for j in jobs} == {
        (SHOP.organization_id, SHOP.shop_id),
        (SECOND.organization_id, SECOND.shop_id),
    }
    assert {j.spec.lock_key for j in jobs} == {
        f"shop:{SHOP.shop_id}:etsy",
        f"shop:{SECOND.shop_id}:etsy",
    }


def test_enqueue_capability_probes__the_next_day__probes_again() -> None:
    queue = MemoryJobQueue()
    directory = Directory(connected=[SHOP])

    enqueue_capability_probes(directory, queue, now=NOW)
    enqueue_capability_probes(directory, queue, now=NOW + timedelta(days=1))

    assert len(queue.of_kind(PROBE_JOB)) == 2


def test_enqueue_refresh_token_checks__one_job_per_connected_shop() -> None:
    queue = MemoryJobQueue()

    enqueue_refresh_token_checks(Directory(connected=[SHOP, SECOND]), queue, now=NOW)

    assert len(queue.of_kind(EXPIRY_JOB)) == 2


@pytest.mark.ac("AC-5.1.6")
def test_enqueue_purges__only_the_shops_the_directory_finds_due_now() -> None:
    queue = MemoryJobQueue()
    directory = Directory(connected=[SHOP], purgeable=[SECOND])

    enqueue_purges(directory, queue, now=NOW)

    [job] = queue.of_kind(PURGE_JOB)
    assert job.spec.shop_id == SECOND.shop_id
    assert directory.asked_at == [NOW]


def test_prune_finished_jobs__drops_succeeded_jobs_older_than_two_weeks_only() -> None:
    queue = MemoryJobQueue()
    for key, finished, state in (
        ("old", NOW - timedelta(days=15), JobState.SUCCEEDED),
        ("recent", NOW - timedelta(days=13), JobState.SUCCEEDED),
        ("dead", NOW - timedelta(days=40), JobState.DEAD),
    ):
        stored = queue.jobs[queue.enqueue(a_job(key=key), now=finished)]
        stored.state, stored.finished_at = state, finished

    pruned = prune_finished_jobs(queue, now=NOW)

    assert pruned == 1
    assert sorted(j.spec.idempotency_key for j in queue.jobs.values()) == ["dead", "recent"]


# --- per-shop jobs ------------------------------------------------------------------------


@pytest.mark.ac("AC-4.5.2")
async def test_probe_shop_daily__reads_the_shop_and_stores_results_as_the_scheduler() -> None:
    vault, probe = vault_with(SHOP), Probe()
    checks = checks_for(vault, probe)

    probed = await probe_shop_daily(checks, SHOP, now=NOW, correlation_id="job-1")

    assert probed is True
    assert [call.split(":")[0] for call in probe.calls] == [
        "get_shop",
        "check_receipt_addresses",
        "list_listings",
    ]
    store = checks.store
    assert isinstance(store, Capabilities)
    [audit] = store.audits
    assert audit.actor == SCHEDULER
    assert audit.after is not None
    assert audit.after["trigger"] == "daily"


async def test_probe_shop_daily__shop_of_another_organization__is_skipped_without_a_call() -> None:
    vault, probe = vault_with(SHOP), Probe()

    probed = await probe_shop_daily(
        checks_for(vault, probe),
        TenantShop(OTHER_ORG, SHOP.shop_id),
        now=NOW,
        correlation_id="job-1",
    )

    assert probed is False
    assert probe.calls == []


@pytest.mark.parametrize("status", [IntegrationStatus.NEEDS_REAUTH, IntegrationStatus.DISCONNECTED])
async def test_probe_shop_daily__shop_no_longer_connected__is_skipped_without_a_call(
    status: IntegrationStatus,
) -> None:
    vault, probe = vault_with(SHOP), Probe()
    vault.rows[SHOP.shop_id] = replace(vault.rows[SHOP.shop_id], status=status)

    probed = await probe_shop_daily(checks_for(vault, probe), SHOP, now=NOW, correlation_id="j")

    assert probed is False
    assert probe.calls == []


async def test_probe_shop_daily__unknown_shop__is_skipped() -> None:
    probed = await probe_shop_daily(checks_for(Vault()), SHOP, now=NOW, correlation_id="j")

    assert probed is False


async def test_probe_shop_daily__overtaken_by_a_disconnect__is_skipped() -> None:
    vault = vault_with(SHOP)
    checks = checks_for(vault)
    store = checks.store
    assert isinstance(store, Capabilities)
    store.accept = False

    probed = await probe_shop_daily(checks, SHOP, now=NOW, correlation_id="j")

    assert probed is False


async def test_check_refresh_token__healthy__changes_nothing() -> None:
    vault = vault_with(SHOP)

    health = await check_refresh_token(vault, SHOP, now=NOW, correlation_id="j")

    assert health is TokenHealth.OK
    assert vault.rows[SHOP.shop_id].status is IntegrationStatus.CONNECTED
    assert vault.audit == []


async def test_check_refresh_token__within_ten_days__reports_expiring_only() -> None:
    vault = vault_with(SHOP, refresh_left=timedelta(days=3))

    health = await check_refresh_token(vault, SHOP, now=NOW, correlation_id="j")

    assert health is TokenHealth.EXPIRING
    assert vault.rows[SHOP.shop_id].status is IntegrationStatus.CONNECTED


async def test_check_refresh_token__expired__marks_needs_reauth_with_an_audit_record() -> None:
    vault = vault_with(SHOP, refresh_left=-timedelta(minutes=1))

    health = await check_refresh_token(vault, SHOP, now=NOW, correlation_id="job-9")

    assert health is TokenHealth.NEEDS_REAUTH
    assert vault.rows[SHOP.shop_id].status is IntegrationStatus.NEEDS_REAUTH
    [audit] = vault.audit
    assert (audit.action, audit.actor, audit.correlation_id) == (
        "shop.needs_reauth",
        SCHEDULER,
        "job-9",
    )
    assert audit.after == {"reason": "refresh_token_expired"}


async def test_check_refresh_token__already_needs_reauth__is_not_marked_again() -> None:
    vault = vault_with(SHOP, refresh_left=-timedelta(days=1))
    vault.rows[SHOP.shop_id] = replace(
        vault.rows[SHOP.shop_id], status=IntegrationStatus.NEEDS_REAUTH
    )

    health = await check_refresh_token(vault, SHOP, now=NOW, correlation_id="j")

    assert health is TokenHealth.NEEDS_REAUTH
    assert vault.audit == []


async def test_check_refresh_token__shop_of_another_organization__is_ignored() -> None:
    vault = vault_with(SHOP, refresh_left=-timedelta(days=1))

    health = await check_refresh_token(
        vault, TenantShop(OTHER_ORG, SHOP.shop_id), now=NOW, correlation_id="j"
    )

    assert health is None
    assert vault.rows[SHOP.shop_id].status is IntegrationStatus.CONNECTED


@pytest.mark.ac("AC-5.1.6")
async def test_purge_disconnected_shop__due__purges_once_and_audits_as_the_scheduler() -> None:
    purges = Purges(due={SHOP.shop_id})

    first = await purge_disconnected_shop(purges, SHOP, now=NOW, correlation_id="job-3")
    again = await purge_disconnected_shop(purges, SHOP, now=NOW, correlation_id="job-3")

    assert first == {"shop_capabilities": 7}
    assert again is None
    [audit] = purges.audits
    assert (audit.action, audit.entity_id, audit.organization_id, audit.actor) == (
        "shop.purged",
        str(SHOP.shop_id),
        ORG,
        SCHEDULER,
    )
