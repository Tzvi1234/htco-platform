"""The stored order history through the API (invented data).

`GET /v1/shops/{shop_id}/orders|stats` say `sync` and answer `source: htco_store` once the
backfill is complete; `POST /v1/shops/{shop_id}/order-sync` lets the Owner or an Admin start a
sync (CSRF, audit). The contract only grows: every earlier field is still there.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest

from htco_application.marketplace import OrderRefund
from htco_application.order_history import OrderSync
from htco_domain.identity import Role
from tests.api.test_etsy_connect_routes import CSRF, PROBLEMS
from tests.api.test_shop_data_routes import World, _world
from tests.etsy_fakes import Pinning
from tests.log_capture import capture_logs
from tests.order_history_fakes import AuditLog, MemoryOrderStore
from tests.shop_data_fakes import NOW, receipt, usd

pytestmark = pytest.mark.anyio

FULL_NAME = "Fakeroute Middle Buyerzz"


class Wired:
    def __init__(self, world: World) -> None:
        self.world = world
        self.store = MemoryOrderStore()
        self.audit = AuditLog()
        self.sync = OrderSync(
            store=self.store,
            source=world.source,
            tokens=Pinning(),
            clock=world.clock,
            audit=self.audit,
        )
        # The app holds this very dashboard; give it the history.
        object.__setattr__(world.dashboard, "history", self.sync)

    async def post(self, headers: dict[str, str] | None = None) -> object:
        return await self.world.http.post(
            f"/v1/shops/{self.world.shop_id}/order-sync",
            headers=CSRF if headers is None else headers,
        )


@pytest.fixture
async def wired() -> AsyncIterator[Wired]:
    async for world in _world():
        refund = OrderRefund(usd(1200), NOW - timedelta(days=1), "damaged", "refunded", "Fk")
        world.source.receipts = [
            *world.source.receipts,
            replace(
                receipt(8801, created=NOW - timedelta(days=400), status="partially refunded"),
                refunds=(refund,),
                buyer_name=FULL_NAME,
            ),
        ]
        yield Wired(world)


async def test_orders__while_syncing__live_with_sync_state_then_from_the_store(
    wired: Wired,
) -> None:
    first = (await wired.world.get("orders", period="all")).json()

    assert first["source"] == "etsy_live"
    assert first["sync"]["state"] == "syncing_history"
    await wired.sync.idle()
    after = await wired.world.get("orders", period="all")

    assert after.status_code == 200, after.text
    body = after.json()
    assert body["source"] == "htco_store"
    assert body["sync"]["state"] == "complete"
    assert body["sync"]["orders_stored"] == 7
    assert body["partial"] is False
    assert {"items", "total", "counts", "buyer_details", "cap", "window_start"} <= body.keys()
    [refunded] = [o for o in body["items"] if o["order_id"] == 8801]
    assert refunded["refunds"] == [
        {
            "amount": {"amount": "12.00", "currency": "USD"},
            "refunded_at": refunded["refunds"][0]["refunded_at"],
            "reason": "damaged",
            "status": "refunded",
            "note": "Fk",
        }
    ]
    assert refunded["refunded_total"] == {"amount": "12.00", "currency": "USD"}
    assert refunded["buyer"] == "Fakeroute B."
    assert FULL_NAME not in after.text
    assert after.headers["cache-control"] == "no-store"


async def test_stats__from_the_store__customers_and_reasons(wired: Wired) -> None:
    await wired.world.get("stats")
    await wired.sync.idle()

    body = (await wired.world.get("stats")).json()

    assert body["source"] == "htco_store"
    sales = body["sales"]
    assert sales["receipts_read"] == 7
    assert sales["customers"] == 1
    assert sales["repeat_customers_percent"] == "0.0"
    assert sales["refunds_and_cancellations"] == [
        {
            "kind": "cancellation",
            "reason": None,
            "orders": 1,
            "amount": {"amount": "0", "currency": "USD"},
        },
        {
            "kind": "refund",
            "reason": "damaged",
            "orders": 1,
            "amount": {"amount": "12.00", "currency": "USD"},
        },
    ]
    assert FULL_NAME not in str(body)


async def test_order_sync__owner__202_runs_and_audits(wired: Wired) -> None:
    with capture_logs() as logs:
        response = await wired.post()
        await wired.sync.idle()

    assert response.status_code == 202, response.text  # type: ignore[attr-defined]
    assert response.json()["state"] == "syncing_history"  # type: ignore[attr-defined]
    assert len(wired.store.orders) == 7
    [record] = wired.audit.records
    assert record.action == "shop.order_sync_requested"  # type: ignore[attr-defined]
    assert FULL_NAME not in logs.raw()
    assert all(method == "list_order_receipts" for method in wired.world.source.operations())


async def test_order_sync__without_csrf__403_and_nothing_runs(wired: Wired) -> None:
    response = await wired.post(headers={})

    assert response.status_code == 403  # type: ignore[attr-defined]
    assert response.json()["type"] == PROBLEMS + "csrf-failed"  # type: ignore[attr-defined]
    assert wired.audit.records == []
    assert not wired.sync.running(uuid.UUID(wired.world.shop_id))


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER])
async def test_order_sync__other_roles__403(wired: Wired, role: Role) -> None:
    wired.world.memberships.role = role

    response = await wired.post()

    assert response.status_code == 403  # type: ignore[attr-defined]
    assert response.json()["type"] == PROBLEMS + "forbidden"  # type: ignore[attr-defined]


async def test_order_sync__unknown_shop__404(wired: Wired) -> None:
    response = await wired.world.http.post(f"/v1/shops/{uuid.uuid4()}/order-sync", headers=CSRF)

    assert response.status_code == 404


async def test_order_sync__not_set_up__503() -> None:
    async for world in _world():
        response = await world.http.post(f"/v1/shops/{world.shop_id}/order-sync", headers=CSRF)

        assert response.status_code == 503
        assert response.json()["type"] == PROBLEMS + "shop-data-unavailable"
