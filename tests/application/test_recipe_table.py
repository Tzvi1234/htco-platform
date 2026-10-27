"""The API channel's recipe table (AUD-06 structural, #147, AC-4).

The channel dispatches by a table of the recipes it implements. At startup the gateway refuses a
catalog binding whose recipe its deployed channel does not implement, unless the binding is
marked disabled; a disabled binding is never routed.
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import ActionOutcome, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_domain.actors import ActorKind, ActorRef
from tests.application.test_action_gateway import ORG, SHOP, ask, office
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.execution_fakes import (
    Approvals,
    Capabilities,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)

pytestmark = pytest.mark.anyio


def gateway(catalog: Any, adapter: Any = None) -> ActionGatewayService:
    registry = office()
    return ActionGatewayService(
        GatewayParts(
            catalog=catalog,
            registry=lambda: registry,
            store=MemoryActionStore(),
            principals=Principals(),
            shops=Shops({SHOP: ORG}),
            approvals=Approvals(),
            adapters={Channel.API: adapter or EtsyApiChannel(FakeMarketplace(), EtsyShops())},
            capabilities=Capabilities({(SHOP, "receipts_read"), (SHOP, "shop_details")}),
            clock=lambda: datetime.now(UTC),
        )
    )


def with_recipe(recipe: str, *, disabled: bool = False) -> Any:
    catalog = real_catalog(office())
    action = catalog.actions["etsy.shop.read"]
    binding = replace(action.bindings[0], recipe=recipe, disabled=disabled)
    actions = {**catalog.actions, "etsy.shop.read": replace(action, bindings=(binding,))}
    return replace(catalog, actions=actions)


def test_the_api_channel__names_the_recipes_it_implements() -> None:
    channel = EtsyApiChannel(FakeMarketplace(), EtsyShops())

    assert channel.recipes >= {"get_shop", "list_receipts", "list_reviews"}


def test_the_real_catalog__starts_with_every_enabled_recipe_implemented() -> None:
    gateway(real_catalog(office()))  # raises if a binding names a recipe nobody implements


def test_a_binding_whose_recipe_no_channel_implements__refused_at_startup() -> None:
    with pytest.raises(ValueError, match=r"etsy\.shop\.read.*not_a_recipe"):
        gateway(with_recipe("not_a_recipe"))


async def test_a_disabled_binding__starts_but_is_never_routed() -> None:
    service = gateway(with_recipe("not_a_recipe", disabled=True))

    result = await service.submit(
        ask(
            "etsy.shop.read",
            actor=ActorRef(ActorKind.SYSTEM, "probe"),
            key="shop:disabled",
            input={},
        )
    )

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "no_channel")


def test_the_inventory_write__is_a_disabled_binding_until_it_is_built() -> None:
    [binding] = real_catalog(office()).actions["etsy.inventory.update"].bindings

    assert binding.disabled
