"""Slice 5.6 through the API: the existing endpoints read the stored order, one order with its
status timeline is a sub-resource of `/orders`, and every answer says `last_synced_at`
(invented data). The contract only grows."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from htco_application.order_history import OrderStatusChange
from htco_application.order_ingest import IngestSource
from htco_domain.identity import Role
from htco_domain.orders import OrderState
from tests.api.test_order_history_routes import FULL_NAME, Wired
from tests.api.test_shop_data_routes import _world, problem
from tests.shop_data_fakes import NOW

pytestmark = pytest.mark.anyio


@pytest.fixture
async def synced() -> AsyncIterator[Wired]:
    async for world in _world():
        wired = Wired(world)
        await world.get("orders")
        await wired.sync.idle()
        world.source.calls.clear()
        yield wired


def first_order(wired: Wired) -> tuple[uuid.UUID, int]:
    return min(wired.store.orders)


@pytest.mark.ac("AC-5.6.3")
@pytest.mark.parametrize("view", ["orders", "overview", "stats"])
async def test_every_answer__has_last_synced_at(synced: Wired, view: str) -> None:
    body = (await synced.world.get(view)).json()

    assert body["last_synced_at"] is not None
    assert body["sync"]["last_synced_at"] == body["last_synced_at"]


@pytest.mark.ac("AC-5.6.4")
async def test_overview__from_the_store__says_so_and_reads_no_receipts(synced: Wired) -> None:
    body = (await synced.world.get("overview")).json()

    assert body["sales_source"] == "htco_store"
    assert "list_order_receipts" not in [call[0] for call in synced.world.source.calls]


@pytest.mark.ac("AC-5.6.5")
@pytest.mark.ac("AC-5.6.3")
async def test_one_order__with_its_timeline_and_sources(synced: Wired) -> None:
    shop, order_id = first_order(synced)
    synced.store.timelines[(shop, order_id)] = [
        OrderStatusChange(None, OrderState.PAID, IngestSource.BACKFILL, NOW - timedelta(days=2)),
        OrderStatusChange(
            OrderState.PAID, OrderState.INGESTED, IngestSource.BACKFILL, NOW - timedelta(days=2)
        ),
    ]

    response = await synced.world.get(f"orders/{order_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order"]["order_id"] == order_id
    assert body["source"] == "htco_store"
    assert body["last_synced_at"] is not None
    timeline = [(e["from_state"], e["to_state"], e["state"], e["source"]) for e in body["timeline"]]
    assert timeline == [
        (None, "paid", "paid", "backfill"),
        ("paid", "ingested", "paid", "backfill"),
    ]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.ac("AC-5.6.1")
async def test_one_order__unknown__404(synced: Wired) -> None:
    problem(await synced.world.get("orders/999999"), "not-found", 404)


@pytest.mark.ac("AC-5.6.1")
async def test_one_order__a_shop_not_granted__404(synced: Wired) -> None:
    synced.world.memberships.role = Role.OPERATIONS
    synced.world.memberships.granted = frozenset()

    problem(await synced.world.get(f"orders/{first_order(synced)[1]}"), "not-found", 404)


@pytest.mark.ac("AC-5.6.1")
async def test_one_order__another_shop_id__404(synced: Wired) -> None:
    response = await synced.world.get(f"orders/{first_order(synced)[1]}", shop_id=str(uuid.uuid4()))

    problem(response, "not-found", 404)


@pytest.mark.ac("AC-5.6.2")
async def test_one_order__viewer__no_buyer_name_tracking_or_note(synced: Wired) -> None:
    synced.world.memberships.role = Role.VIEWER

    response = await synced.world.get(f"orders/{first_order(synced)[1]}")

    body = response.json()
    assert body["buyer_details"] is False
    assert body["order"]["buyer"] is None
    assert all(s["tracking_code"] is None for s in body["order"]["shipments"])
    assert FULL_NAME not in response.text
