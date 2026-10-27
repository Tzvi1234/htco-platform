"""The API process's fallback flag (slice 5.4, AC-5.5.5 prepared): with requests set, a stale
read and the manual sync ask the worker's queue and never run the sync in this process."""

from dataclasses import dataclass, field, replace
from datetime import datetime

import pytest

from htco_application.audit import AuditRecord
from htco_application.order_history import OrderSync, adapter_pages, request_order_sync
from htco_application.shop_dashboard import shop_orders
from htco_application.shop_integrations import ShopIntegration
from tests.application.test_shop_dashboard import SHOP, World, auth
from tests.etsy_fakes import Pinning
from tests.order_history_fakes import AuditLog, MemoryOrderStore

pytestmark = pytest.mark.anyio


@dataclass
class Requests:
    asked: list[tuple[ShopIntegration, bool]] = field(default_factory=list)

    async def request(self, integration: ShopIntegration, *, now: datetime, force: bool) -> bool:  # noqa: ARG002 - the port
        self.asked.append((integration, force))
        return True


def _world(requests: Requests) -> tuple[World, OrderSync]:
    world = World()
    sync = OrderSync(
        store=MemoryOrderStore(),
        pages=adapter_pages(world.source, Pinning()),
        clock=world.clock,
        audit=AuditLog(),
        requests=requests,
    )
    world.dashboard = replace(world.dashboard, history=sync)
    return world, sync


@pytest.mark.ac("AC-5.5.5")
async def test_a_stale_read__asks_the_queue_and_runs_nothing_here() -> None:
    requests = Requests()
    world, sync = _world(requests)

    await shop_orders(world.dashboard, auth(), SHOP, now=world.clock())

    assert [(i.shop_id, force) for i, force in requests.asked] == [(SHOP, False)]
    assert not sync.running(SHOP)
    await sync.idle()
    assert sync.store.pages_saved == 0  # type: ignore[attr-defined]


async def test_the_manual_sync__asks_the_queue_forced_and_is_audited() -> None:
    requests = Requests()
    world, sync = _world(requests)

    await request_order_sync(
        sync, world.vault, auth(), SHOP, now=world.clock(), correlation_id="corr-manual"
    )

    assert [force for _, force in requests.asked] == [True]
    assert not sync.running(SHOP)
    assert isinstance(sync.audit, AuditLog)
    [record] = sync.audit.records
    assert isinstance(record, AuditRecord)
    assert record.action == "shop.order_sync_requested"
