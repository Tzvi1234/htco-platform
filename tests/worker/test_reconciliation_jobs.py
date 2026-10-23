"""Reconciliation's schedules in the worker (slice 5.5): off unless named, a fan-out per period,
and the per-shop job starting the right run of Etsy Order Intake."""

import uuid
from datetime import timedelta

import pytest

from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.maintenance import TenantShop
from htco_application.order_intake import (
    ORDER_SYNC_JOB,
    RECONCILE_SCHEDULE,
    SWEEP_SCHEDULE,
    EngineSyncTrigger,
)
from htco_domain.automation import TriggerKind
from htco_worker.main import reconciliation_handlers
from htco_worker.settings import SCHEDULES, WorkerSettings
from tests.application.test_order_intake import Engine
from tests.job_fakes import MemoryJobQueue
from tests.order_intake_fakes import NOW

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("6e000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("6f000000-0000-0000-0000-000000000001")


class Directory:
    def connected_shops(self) -> list[TenantShop]:
        return [TenantShop(ORG, SHOP)]

    def purgeable_shops(self, *, now: object) -> list[TenantShop]:  # noqa: ARG002 - the port
        return []


def test_settings__the_reconciliation_schedules_are_known() -> None:
    assert {RECONCILE_SCHEDULE.kind, SWEEP_SCHEDULE.kind} <= set(SCHEDULES)
    settings = WorkerSettings.model_validate(
        {
            "environment": "test",
            "database_url": "postgresql://htco@db.invalid/htco",
            "worker_schedules": f"{RECONCILE_SCHEDULE.kind},{SWEEP_SCHEDULE.kind}",
        }
    )
    assert settings.schedules == (RECONCILE_SCHEDULE.kind, SWEEP_SCHEDULE.kind)


def test_handlers__only_the_named_schedules() -> None:
    queue = MemoryJobQueue()
    assert reconciliation_handlers((), queue=queue, directory=Directory()) == {}
    handlers = reconciliation_handlers(
        (RECONCILE_SCHEDULE.kind,), queue=queue, directory=Directory()
    )
    assert set(handlers) == {RECONCILE_SCHEDULE.kind}


async def test_handlers__the_fan_out_enqueues_one_job_per_shop() -> None:
    queue = MemoryJobQueue()
    handlers = reconciliation_handlers(
        (RECONCILE_SCHEDULE.kind, SWEEP_SCHEDULE.kind), queue=queue, directory=Directory()
    )
    job = ClaimedJob(
        id=uuid.uuid7(), kind=SWEEP_SCHEDULE.kind, idempotency_key="k", payload={},
        organization_id=None, shop_id=None, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW + timedelta(minutes=1),
    )  # fmt: skip

    await handlers[SWEEP_SCHEDULE.kind](job)

    [stored] = queue.jobs.values()
    assert stored.spec.kind == ORDER_SYNC_JOB
    assert stored.spec.payload == {"reason": "sweep"}


@pytest.mark.parametrize(
    ("reason", "mode", "trigger"),
    [
        ("reconcile", "reconcile", TriggerKind.SCHEDULE),
        ("sweep", "sweep", TriggerKind.SCHEDULE),
        ("stale", "history", TriggerKind.EVENT),
        ("manual", "history", TriggerKind.EVENT),
        ("unknown", "history", TriggerKind.EVENT),
    ],
)
async def test_sync_job__starts_the_run_its_reason_names(
    reason: str, mode: str, trigger: TriggerKind
) -> None:
    engine = Engine()
    job = ClaimedJob(
        id=uuid.uuid7(), kind=ORDER_SYNC_JOB, idempotency_key=f"{ORDER_SYNC_JOB}:{SHOP}:{reason}:p",
        payload={"reason": reason}, organization_id=ORG, shop_id=SHOP, lock_key=None,
        attempts=1, retry=DEFAULT_RETRY, lease_expires_at=NOW,
    )  # fmt: skip

    await EngineSyncTrigger(engine).run(job)

    [request] = engine.requests
    assert request.payload["mode"] == mode
    assert request.trigger is trigger
    assert request.idempotency_key.startswith(f"etsy:{SHOP}:{mode}:")
