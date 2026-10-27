"""The shared kernel (#140): lock keys and the per-shop fan-out."""

import uuid
from datetime import UTC, datetime

import pytest

from htco_application.jobs import fan_out_per_shop
from htco_application.kernel import EVENTS_DESTINATION, LockKeys, TenantShop, run_inline
from tests.job_fakes import MemoryJobQueue

NOW = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
SHOP_ID = uuid.UUID("0190a000-0000-7000-8000-000000000001")
ORG_ID = uuid.UUID("0190a000-0000-7000-8000-0000000000aa")


@pytest.mark.ac("AC-140.3")
def test_lock_keys__shop_etsy__is_the_maintenance_lock() -> None:
    assert LockKeys.shop_etsy(SHOP_ID) == f"shop:{SHOP_ID}:etsy"


@pytest.mark.ac("AC-140.3")
def test_lock_keys__shop_orders__is_the_order_sync_lock() -> None:
    assert LockKeys.shop_orders(SHOP_ID) == f"shop:{SHOP_ID}:orders"


def test_events_destination__is_the_outbox_publish_destination() -> None:
    assert EVENTS_DESTINATION == "events.publish"


@pytest.mark.anyio
async def test_run_inline__returns_the_work_result() -> None:
    assert await run_inline(lambda: 42) == 42


@pytest.mark.ac("AC-140.3")
def test_fan_out_per_shop__one_job_per_shop__with_its_key_lock_and_payload() -> None:
    other = uuid.UUID("0190a000-0000-7000-8000-000000000002")
    shops = [TenantShop(ORG_ID, SHOP_ID), TenantShop(ORG_ID, other)]
    queue = MemoryJobQueue()

    count = fan_out_per_shop(
        queue,
        shops,
        kind="etsy.capability_probe",
        key=lambda shop: f"probe:{shop.shop_id}",
        lock=LockKeys.shop_etsy,
        payload={"reason": "test"},
        run_at=NOW,
        correlation=lambda shop: f"c:{shop.shop_id}",
        now=NOW,
    )

    specs = sorted((job.spec for job in queue.jobs.values()), key=lambda s: s.idempotency_key)
    assert count == 2
    assert [s.idempotency_key for s in specs] == [f"probe:{SHOP_ID}", f"probe:{other}"]
    assert [s.lock_key for s in specs] == [LockKeys.shop_etsy(SHOP_ID), LockKeys.shop_etsy(other)]
    assert {s.organization_id for s in specs} == {ORG_ID}
    assert [s.shop_id for s in specs] == [SHOP_ID, other]
    assert all(s.payload == {"reason": "test"} for s in specs)
    assert all(s.run_at == NOW for s in specs)
    assert [s.correlation_id for s in specs] == [f"c:{SHOP_ID}", f"c:{other}"]


@pytest.mark.ac("AC-140.3")
def test_fan_out_per_shop__no_lock__enqueues_unlocked_jobs() -> None:
    queue = MemoryJobQueue()

    fan_out_per_shop(
        queue,
        [TenantShop(ORG_ID, SHOP_ID)],
        kind="fulfillment.inspect_shop",
        key=lambda shop: f"k:{shop.shop_id}",
        lock=None,
        now=NOW,
    )

    (job,) = queue.jobs.values()
    assert job.spec.lock_key is None
    assert job.spec.correlation_id is None
    assert job.spec.payload == {}


def test_fan_out_per_shop__no_shops__enqueues_nothing() -> None:
    queue = MemoryJobQueue()

    assert fan_out_per_shop(queue, [], kind="shops.purge", key=str, lock=None, now=NOW) == 0
    assert queue.jobs == {}
