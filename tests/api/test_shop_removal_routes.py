"""Removing a shop through the API, with fakes: `DELETE /v1/shops/{shop_id}`.

The Owner with MFA and a recent step-up, from the web app (CSRF). The shop is disconnected (its
tokens deleted) and every piece of its stored data goes at once; nothing is sent to Etsy. The
audit record keeps no buyer data. Repeating it answers 404 and changes nothing. The database
side (one transaction, the cascades) is proven in tests/integration and the pgTAP tests.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from htco_application.audit import AuditRecord
from htco_application.shop_integrations import RemovableShop, RemovedShop
from htco_domain.identity import Role
from tests.api.test_capability_disconnect_routes import World, _world
from tests.api.test_etsy_connect_routes import CSRF, PROBLEMS
from tests.etsy_fakes import AuditLog, Vault

pytestmark = pytest.mark.anyio


@dataclass
class Remover:
    """`ShopRemover` over the fake vault: the shop is its integration row here."""

    vault: Vault
    audit: AuditLog
    removed: list[uuid.UUID] = field(default_factory=list)

    async def find(self, shop_id: uuid.UUID) -> RemovableShop | None:
        row = self.vault.rows.get(shop_id)
        if row is None:
            return None
        return RemovableShop(
            id=shop_id,
            organization_id=row.organization_id,
            name="FakeCraftsShop",
            status="active",
            integration_status=row.status,
        )

    async def remove(self, shop: RemovableShop, *, audit: AuditRecord) -> RemovedShop | None:
        if self.vault.rows.pop(shop.id, None) is None:
            return None
        self.removed.append(shop.id)
        await self.audit.append(audit)
        return RemovedShop(shop_id=shop.id, orders_deleted=3)


async def _removal_world(**options: object) -> AsyncIterator[World]:
    async for world in _world(shop_remover=Remover, **options):  # type: ignore[arg-type]
        yield world


@pytest.fixture
async def world() -> AsyncIterator[World]:
    async for created in _removal_world():
        yield created


async def _remove(world: World, headers: dict[str, str] = CSRF) -> object:
    return await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=headers))


async def test_remove__owner_with_step_up__204_tokens_and_shop_gone_audited(world: World) -> None:
    response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))

    assert response.status_code == 204, response.text
    assert uuid.UUID(world.shop_id) not in world.vault.rows
    [record] = [r for r in world.audit.records if r.action == "shop.removed"]
    assert record.entity_id == world.shop_id
    assert record.before == {
        "name": "FakeCraftsShop",
        "status": "active",
        "integration_status": "connected",
    }
    assert record.after == {"removed": True, "etsy_revocation": "not_offered_by_etsy"}
    assert world.probe.calls == []  # nothing is sent to Etsy
    card = (await world.http.get("/v1/shop-integrations")).json()["items"]
    assert card == []


async def test_remove__twice__404_the_second_time_and_one_audit(world: World) -> None:
    await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))

    again = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))

    assert again.status_code == 404
    assert again.json()["type"] == PROBLEMS + "not-found"
    assert [r.action for r in world.audit.records].count("shop.removed") == 1


async def test_remove__a_disconnected_shop__still_removed(world: World) -> None:
    assert (
        await world.send(
            world.http.post(f"/v1/shop-integrations/{world.shop_id}/disconnect", headers=CSRF)
        )
    ).status_code == 200

    response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))

    assert response.status_code == 204
    [record] = [r for r in world.audit.records if r.action == "shop.removed"]
    assert record.before is not None
    assert record.before["integration_status"] == "disconnected"


async def test_remove__without_csrf__403_and_kept(world: World) -> None:
    response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}"))

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "csrf-failed"
    assert uuid.UUID(world.shop_id) in world.vault.rows


async def test_remove__step_up_older_than_ten_minutes__403_step_up_required(
    world: World,
) -> None:
    world.clock.now += timedelta(minutes=11)

    response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))

    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "step-up-required"
    assert uuid.UUID(world.shop_id) in world.vault.rows


async def test_remove__without_mfa__403_mfa_required() -> None:
    async for world in _removal_world(mfa=False):
        response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))
        assert response.status_code == 403
        assert response.json()["type"] == PROBLEMS + "mfa-required"
        assert uuid.UUID(world.shop_id) in world.vault.rows


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.VIEWER])
async def test_remove__not_the_owner__refused_and_kept(role: Role) -> None:
    async for world in _removal_world(role=role):
        response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))
        assert response.status_code in {403, 404}
        assert uuid.UUID(world.shop_id) in world.vault.rows
        assert not [r for r in world.audit.records if r.action == "shop.removed"]


async def test_remove__unknown_shop__404(world: World) -> None:
    response = await world.send(world.http.delete(f"/v1/shops/{uuid.uuid4()}", headers=CSRF))

    assert response.status_code == 404


async def test_remove__not_configured__503() -> None:
    async for world in _world():
        response = await world.send(world.http.delete(f"/v1/shops/{world.shop_id}", headers=CSRF))
        assert response.status_code == 503
        assert response.json()["type"] == PROBLEMS + "etsy-unavailable"
