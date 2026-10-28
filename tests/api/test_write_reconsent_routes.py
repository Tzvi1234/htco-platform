"""`POST /v1/shops/{shop_id}/owner-writes/{key}/reconnect` (AC-6.8.2), through the web app.

The Owner with a second factor and a recent step-up starts the Etsy connect flow asking for the
read scopes plus the write's one scope; the callback stores the granted scopes, and the console
then shows the scope as held. The normal connect stays read-only. Fakes only: no Etsy call.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from htco_application.etsy_connect import ETSY_CONNECT_SCOPES
from htco_application.owner_writes import OwnerWriteServices
from htco_domain.identity import Role
from tests.api.test_capability_disconnect_routes import World, _world
from tests.api.test_etsy_connect_routes import CSRF, PROBLEMS
from tests.etsy_fakes import GOOD_CODE, Vault
from tests.owner_write_fakes import World as Writes

pytestmark = pytest.mark.anyio


class VaultFacts:
    """The owner writes' shop facts read from the fake vault: what the callback stored."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        row = self._vault.rows.get(shop_id)
        return None if row is None else row.organization_id

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]:
        row = self._vault.rows.get(shop_id)
        return frozenset() if row is None else frozenset(row.scopes)


def services(vault: Vault) -> OwnerWriteServices:
    from dataclasses import replace  # noqa: PLC0415

    facts = VaultFacts(vault)
    return replace(Writes().services, shops=facts)


async def _reconsent_world(**options: Any) -> AsyncIterator[World]:
    async for world in _world(owner_writes=services, **options):
        yield world


def scopes_of(url: str) -> set[str]:
    return set(httpx.URL(url).params["scope"].split(" "))


async def reconnect(world: World, key: str = "tracking_upload") -> httpx.Response:
    return await world.send(
        world.http.post(f"/v1/shops/{world.shop_id}/owner-writes/{key}/reconnect", headers=CSRF)
    )


@pytest.mark.ac("AC-6.8.2")
@pytest.mark.parametrize(
    ("key", "scope"),
    [("tracking_upload", "transactions_w"), ("mark_shipped", "transactions_w"),
     ("vacation_mode", "shops_w"), ("holiday_preference", "shops_w")],
)  # fmt: skip
async def test_reconnect__asks_for_the_read_scopes_plus_exactly_the_writes_scope(
    key: str, scope: str
) -> None:
    async for world in _reconsent_world():
        answer = await reconnect(world, key)

        assert answer.status_code == 200, answer.text
        assert answer.headers["Cache-Control"] == "no-store"
        assert scopes_of(answer.json()["authorization_url"]) == ETSY_CONNECT_SCOPES | {scope}
        [record] = [r for r in world.audit.records if r.action == "shop.write_reconsent_started"]
        assert record.after == {"scope": scope, "write": key}


@pytest.mark.ac("AC-6.8.2")
async def test_reconnect__the_callback_stores_the_scope_and_the_console_shows_it_held() -> None:
    async for world in _reconsent_world():
        before = (await world.http.get(f"/v1/shops/{world.shop_id}/owner-writes")).json()
        tracking = before["items"][0]
        assert tracking["reconsent_required"] is True
        assert tracking["reconnect_url"] == (
            f"/v1/shops/{world.shop_id}/owner-writes/tracking_upload/reconnect"
        )

        started = await reconnect(world)
        state = httpx.URL(started.json()["authorization_url"]).params["state"]
        done = await world.send(
            world.http.post(
                "/v1/integrations/etsy/callback",
                json={"code": GOOD_CODE, "state": state},
                headers=CSRF,
            )
        )
        after = (await world.http.get(f"/v1/shops/{world.shop_id}/owner-writes")).json()

        assert done.status_code == 200, done.text
        assert "transactions_w" in done.json()["integration"]["scopes"]
        rows = {row["key"]: row for row in after["items"]}
        assert rows["tracking_upload"]["scope_granted"] is True
        assert rows["tracking_upload"]["reconnect_url"] is None
        assert rows["vacation_mode"]["scope_granted"] is False  # shops_w was not asked for


async def test_the_normal_connect__stays_read_only() -> None:
    async for world in _reconsent_world():
        started = await world.http.post("/v1/shops/etsy/connect", headers=CSRF)

        assert scopes_of(started.json()["authorization_url"]) == ETSY_CONNECT_SCOPES


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.VIEWER])
async def test_reconnect__anyone_but_the_owner_403(role: Role) -> None:
    async for world in _reconsent_world(role=role):
        answer = await reconnect(world)

        assert answer.status_code == 403
        assert answer.json()["type"] == PROBLEMS + "forbidden"
        assert not any(r.action == "shop.write_reconsent_started" for r in world.audit.records)


async def test_reconnect__a_stale_step_up_403_and_csrf_is_required() -> None:
    async for world in _reconsent_world():
        no_csrf = await world.send(
            world.http.post(f"/v1/shops/{world.shop_id}/owner-writes/tracking_upload/reconnect")
        )
        world.clock.now += timedelta(minutes=11)
        stale = await reconnect(world)

        assert no_csrf.status_code == 403
        assert no_csrf.json()["type"] == PROBLEMS + "csrf-failed"
        assert stale.status_code == 403
        assert stale.json()["type"] == PROBLEMS + "step-up-required"


async def test_reconnect__another_organizations_shop_404() -> None:
    async for world in _reconsent_world():
        answer = await world.send(
            world.http.post(
                f"/v1/shops/{uuid.uuid4()}/owner-writes/tracking_upload/reconnect", headers=CSRF
            )
        )

        assert answer.status_code == 404
