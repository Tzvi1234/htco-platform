"""The restricted Etsy reads at the Action Gateway (AUD-06, #147 part B; sage2 on #207).

`etsy.order_history.read` (buyer data) and `etsy.shipments.read` (tracking codes) name
`actor_kinds = ["automation"]` and are granted to one automation each (`RESTRICTED_GRANTS`). The
gateway refuses any other actor kind before routing, and refuses such a grant per request,
whatever the registry provider returns (the worker's reads Postgres). The startup refusal is the
registry loader's (`Registry.from_documents`, #207): see `tests/worker/test_worker_gateway.py`.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest

from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import ActionOutcome, Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.workforce.registry import Registry, parse_automation, parse_employee
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.workforce import Readiness
from tests.application.test_action_gateway import ORG, SHOP, USER, ask, office
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.application.workforce_fixtures import employee_document
from tests.automation_fakes import engine_document
from tests.execution_fakes import (
    Approvals,
    Capabilities,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)

pytestmark = pytest.mark.anyio

RESTRICTED = ("etsy.order_history.read", "etsy.shipments.read")
OWNER_OF = {"etsy.order_history.read": "etsy-order-intake", "etsy.shipments.read": "tracking-sync"}


def granted(registry: Registry, automation_id: str, *actions: str) -> Registry:
    """The registry with these grants added, bypassing the loader's own check (as a registry
    provider could)."""
    automation = registry.automations.get(automation_id) or parse_automation(
        engine_document(automation_id, allowed_actions=[])
    )
    automation = replace(automation, allowed_actions=automation.allowed_actions | set(actions))
    tools = {
        name: replace(tool, readiness=Readiness.ACTIVE) if name in actions else tool
        for name, tool in registry.tools.items()
    }
    return replace(
        registry,
        tools=MappingProxyType(tools),
        automations=MappingProxyType({**registry.automations, automation_id: automation}),
    )


def legal() -> Registry:
    registry = granted(office(), "etsy-order-intake", "etsy.order_history.read")
    return granted(registry, "tracking-sync", "etsy.shipments.read")


class Office:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.channel = EtsyApiChannel(FakeMarketplace(), EtsyShops())
        self.gateway = ActionGatewayService(
            GatewayParts(
                catalog=real_catalog(registry),
                registry=lambda: self.registry,
                store=MemoryActionStore(),
                principals=Principals(),
                shops=Shops({SHOP: ORG}),
                approvals=Approvals(),
                adapters={Channel.API: self.channel},
                capabilities=Capabilities({(SHOP, "receipts_read")}),
                clock=lambda: datetime.now(UTC),
            )
        )


def read(action: str, actor: ActorRef, key: str = "r:1") -> Any:
    return ask(action, actor=actor, key=f"{action}:{key}", input={"limit": 25})


@pytest.mark.parametrize("action", RESTRICTED)
def test_the_catalog__the_restricted_reads_name_automations_only(action: str) -> None:
    assert real_catalog(legal()).actions[action].actor_kinds == frozenset({ActorKind.AUTOMATION})


@pytest.mark.parametrize("action", RESTRICTED)
@pytest.mark.parametrize(
    "actor",
    [
        ActorRef(ActorKind.USER, str(USER)),
        ActorRef(ActorKind.EMPLOYEE, "eitan"),
        ActorRef(ActorKind.SYSTEM, "probe"),
    ],
    ids=["user", "employee", "system"],
)
async def test_another_actor_kind__refused_before_routing(action: str, actor: ActorRef) -> None:
    office_ = Office(legal())

    result = await office_.gateway.submit(read(action, actor))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")
    assert result.channel is None


async def test_an_employee_granted_the_tool__still_refused_by_kind() -> None:
    registry = legal()
    employee = parse_employee(employee_document("eitan", allowed_tools=list(RESTRICTED)))
    registry = replace(registry, employees=MappingProxyType({"eitan": employee}))
    office_ = Office(legal())
    office_.registry = registry

    for action in RESTRICTED:
        result = await office_.gateway.submit(read(action, ActorRef(ActorKind.EMPLOYEE, "eitan")))
        assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")


@pytest.mark.parametrize("action", RESTRICTED)
async def test_its_own_automation__passes_authorization(action: str) -> None:
    office_ = Office(legal())

    result = await office_.gateway.submit(
        read(action, ActorRef(ActorKind.AUTOMATION, OWNER_OF[action]))
    )

    # Authorized: it reaches routing, where the binding stays disabled until its recipe lands.
    assert result.error_code != "forbidden"


@pytest.mark.parametrize("action", RESTRICTED)
async def test_a_grant_that_appears_after_startup__refused_per_request(action: str) -> None:
    other = "etsy-order-intake" if OWNER_OF[action] == "tracking-sync" else "tracking-sync"
    office_ = Office(legal())
    office_.registry = granted(office_.registry, other, action)  # the provider changed

    result = await office_.gateway.submit(read(action, ActorRef(ActorKind.AUTOMATION, other)))

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "forbidden")
