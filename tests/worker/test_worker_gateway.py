"""The worker's Action Gateway (slice 5.4): real for reads behind `HTCO_WORKER_ACTION_GATEWAY`,
and a write never reaches a channel (the shared approval contract waits for a decision no one
can give in the worker)."""

import re
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from htco_application.approvals import APPROVAL_DECIDED, ApprovalResumer, NoApprovals
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRef,
    ActionRequest,
    Channel,
    UnavailableGateway,
)
from htco_application.execution.service import (
    ActionGatewayService,
    GatewayParts,
    Principals,
    ShopOwners,
)
from htco_application.order_intake import ORDER_SYNC_JOB
from htco_application.workforce.registry import RegistryError, parse_automation
from htco_domain.actors import ActorKind, ActorRef
from htco_persistence.database import Database
from htco_worker.main import (
    NoUsers,
    approval_event_subscribers,
    order_sync_handlers,
    worker_engine,
    worker_gateway,
)
from htco_worker.settings import WorkerSettings
from tests.application.test_action_gateway import NOW, ORG, SHOP, office
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.automation_fakes import engine_document
from tests.execution_fakes import Capabilities, MemoryActionStore
from tests.execution_fakes import real_catalog as catalog_of

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "postgresql://htco@db.invalid/htco"


def _settings(**values: object) -> WorkerSettings:
    return WorkerSettings.model_validate(
        {"environment": "test", "database_url": DATABASE, "worker_health_port": 0} | values
    )


def test_settings__the_gateway_is_on_for_reads_by_default() -> None:
    settings = _settings()
    assert settings.worker_action_gateway == "reads"
    assert settings.registry_path == Path("registry")


def test_gateway__off__refuses_every_action() -> None:
    gateway = worker_gateway(
        _settings(worker_action_gateway="off"),
        Database(DATABASE),
        object(),  # type: ignore[arg-type]
    )
    assert isinstance(gateway, UnavailableGateway)


def test_gateway__without_the_etsy_app__refuses_every_action() -> None:
    assert isinstance(worker_gateway(_settings(), Database(DATABASE), None), UnavailableGateway)


@pytest.mark.parametrize(
    ("automation", "action"),
    [("tracking-sync", "etsy.order_history.read"), ("etsy-order-intake", "etsy.shipments.read")],
)
def test_gateway__a_registry_granting_a_restricted_read_elsewhere__refused_at_startup(
    tmp_path: Path, automation: str, action: str
) -> None:
    """AUD-06 (#147): the worker refuses to start on such a grant, before it reads Postgres."""
    registry = tmp_path / "registry"
    shutil.copytree(ROOT / "registry", registry)
    path = registry / "automations" / f"{automation}.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("allowed_actions = [", f'allowed_actions = ["{action}", ', 1),
        encoding="utf-8",
    )
    assert action in path.read_text(encoding="utf-8")
    settings = _settings(
        registry_path=registry, action_schemas_path=ROOT / "packages" / "contracts" / "actions"
    )

    with pytest.raises(RegistryError, match=rf"{automation}.*{re.escape(action)}"):
        worker_gateway(settings, Database(DATABASE), FakeMarketplace())  # type: ignore[arg-type]


def test_gateway__on__the_real_gateway_with_the_registry_and_contracts() -> None:
    settings = _settings(
        registry_path=ROOT / "registry",
        action_schemas_path=ROOT / "packages" / "contracts" / "actions",
    )
    gateway = worker_gateway(settings, Database(DATABASE), FakeMarketplace())  # type: ignore[arg-type]
    assert isinstance(gateway, ActionGatewayService)
    # AUD-03: the explicit null gate, never an in-memory approval service (nothing approves).
    assert isinstance(gateway._p.approvals, NoApprovals)


@pytest.mark.ac("AUD-03.2")
def test_engine__decisions_from_any_process__reach_the_resumer() -> None:
    subscribers = approval_event_subscribers(worker_engine(Database(DATABASE)))
    assert isinstance(subscribers[APPROVAL_DECIDED], ApprovalResumer)


async def test_write__through_the_worker_parts__waits_and_never_reaches_a_channel() -> None:
    """The worker's approval gate and user ports with the real catalog: a write granted to the
    automation still waits for an approval, and the API channel is never called."""
    actor = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")
    registry = office()
    automation = parse_automation(
        engine_document("etsy-order-intake", allowed_actions=["etsy.inventory.update"])
    )
    registry = replace(
        registry,
        automations=MappingProxyType({"etsy-order-intake": automation}),
    )
    marketplace = FakeMarketplace()
    users = NoUsers()
    gateway = ActionGatewayService(
        GatewayParts(
            catalog=catalog_of(registry),
            registry=lambda: registry,
            store=MemoryActionStore(),
            principals=users,
            shops=users,
            approvals=NoApprovals(),
            adapters={Channel.API: EtsyApiChannel(marketplace, EtsyShops())},
            capabilities=Capabilities({(SHOP, "receipts_read")}),
            clock=lambda: NOW,
        )
    )

    result = await gateway.submit(
        ActionRequest(
            action=ActionRef("etsy.inventory.update", 1),
            actor=actor,
            organization_id=ORG,
            shop_id=SHOP,
            idempotency_key=f"write:{uuid.uuid4()}",
            correlation_id="c",
            input={"listing_id": 1, "products": []},
        )
    )

    assert result.outcome is ActionOutcome.PENDING_APPROVAL
    assert marketplace.calls == []


async def test_write__a_user_actor_in_the_worker_is_unknown() -> None:
    users = NoUsers()
    principals: Principals = users
    owners: ShopOwners = users
    assert principals.principal(uuid.uuid4(), ORG) is None
    assert owners.organization_of_shop(SHOP) is None


def test_engine_and_sync_job__wired() -> None:
    engine = worker_engine(Database(DATABASE))
    assert set(order_sync_handlers(engine)) == {ORDER_SYNC_JOB}
