"""Reconciliation in the worker (slice 5.5): an hourly tick, off unless named, that enqueues the
day's configured runs (twice a day by default), and the per-shop job starting the right run of
Etsy Order Intake."""

import base64
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.maintenance import TenantShop
from htco_application.order_intake import (
    ORDER_SYNC_JOB,
    RECONCILIATION_TICK,
    EngineSyncTrigger,
)
from htco_domain.automation import TriggerKind
from htco_worker.main import reconciliation_handlers
from htco_worker.settings import SCHEDULES, WorkerSettings
from tests.application.test_order_intake import Engine
from tests.job_fakes import MemoryJobQueue
from tests.order_intake_fakes import NOW

pytestmark = pytest.mark.anyio

ISRAEL = ZoneInfo("Asia/Jerusalem")
ORG = uuid.UUID("6e000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("6f000000-0000-0000-0000-000000000001")


class Directory:
    def connected_shops(self) -> list[TenantShop]:
        return [TenantShop(ORG, SHOP)]

    def purgeable_shops(self, *, now: object) -> list[TenantShop]:  # noqa: ARG002 - the port
        return []


VAULT_KEY = "k1:" + base64.b64encode(bytes(32)).decode()


def _settings(**values: object) -> WorkerSettings:
    return WorkerSettings.model_validate(
        {
            "environment": "test",
            "database_url": "postgresql://htco@db.invalid/htco",
            "token_vault_key": VAULT_KEY,
            "etsy_keystring": "fakekeystring",
            "etsy_shared_secret": "fake-shared-secret",
        }
        | values
    )


def test_settings__the_reconciliation_tick_is_known_and_twice_a_day_by_default() -> None:
    assert RECONCILIATION_TICK.kind in SCHEDULES
    settings = _settings(worker_schedules=RECONCILIATION_TICK.kind)
    assert settings.schedules == (RECONCILIATION_TICK.kind,)
    assert settings.reconciliation_times == (time(6, 0), time(18, 0))
    assert settings.reconciliation_zone == ZoneInfo("Asia/Jerusalem")


def test_settings__a_configured_schedule__and_an_invalid_one_refused() -> None:
    assert _settings(reconciliation_schedule="05:00").reconciliation_times == (time(5, 0),)
    with pytest.raises(ValueError, match="HTCO_RECONCILIATION_SCHEDULE"):
        _settings(reconciliation_schedule="noon")
    assert _settings(reconciliation_tz="UTC").reconciliation_zone == ZoneInfo("UTC")
    with pytest.raises(ValueError, match="HTCO_RECONCILIATION_TZ"):
        _settings(reconciliation_tz="Mars/Olympus")


@pytest.mark.parametrize("missing", ["token_vault_key", "etsy_keystring"])
def test_settings__the_tick_without_the_etsy_app_or_vault__refused(missing: str) -> None:
    """Codex review of #75: without them the worker installs no handler and would crash."""
    values: dict[str, object] = {"worker_schedules": RECONCILIATION_TICK.kind, missing: None}
    if missing == "etsy_keystring":
        values["etsy_shared_secret"] = None
    with pytest.raises(ValueError, match=RECONCILIATION_TICK.kind):
        _settings(**values)


def test_handlers__only_when_the_tick_is_named() -> None:
    queue = MemoryJobQueue()
    times = (time(6, 0), time(18, 0))
    assert (
        reconciliation_handlers((), queue=queue, directory=Directory(), times=times, tz=ISRAEL)
        == {}
    )
    handlers = reconciliation_handlers(
        (RECONCILIATION_TICK.kind,), queue=queue, directory=Directory(), times=times, tz=ISRAEL
    )
    assert set(handlers) == {RECONCILIATION_TICK.kind}


@pytest.mark.parametrize(
    ("at", "reason"),
    [
        (datetime(2026, 10, 24, 3, 5, tzinfo=UTC), "reconcile"),
        (datetime(2026, 10, 24, 15, 5, tzinfo=UTC), "sweep"),
        (datetime(2026, 10, 24, 9, 5, tzinfo=UTC), None),
    ],
)
async def test_tick__enqueues_the_due_run_only(at: datetime, reason: str | None) -> None:
    queue = MemoryJobQueue()
    handlers = reconciliation_handlers(
        (RECONCILIATION_TICK.kind,), queue=queue, directory=Directory(),
        times=(time(6, 0), time(18, 0)), tz=ISRAEL, clock=lambda: at,
    )  # fmt: skip
    job = ClaimedJob(
        id=uuid.uuid7(), kind=RECONCILIATION_TICK.kind, idempotency_key="k", payload={},
        organization_id=None, shop_id=None, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW + timedelta(minutes=1),
    )  # fmt: skip

    await handlers[RECONCILIATION_TICK.kind](job)

    reasons = [j.spec.payload["reason"] for j in queue.jobs.values()]
    assert reasons == ([] if reason is None else [reason])
    # Each job waits for its slot: 06:00 or 18:00 Israel time, not the tick's minute.
    assert all(j.run_at.minute == 0 for j in queue.jobs.values())
    assert all(j.spec.kind == ORDER_SYNC_JOB for j in queue.jobs.values())


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
