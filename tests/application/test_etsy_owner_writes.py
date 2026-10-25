"""The owner's Etsy writes on the Action Gateway (slices 6.6 and 6.8, vacation mode from 7.12).

The owner's decision of 2026-10-23: the first Etsy writes are the tracking upload
(`etsy.shipments.create`, scope `transactions_w`) and vacation mode (`etsy.shop.update`, scope
`shops_w`), triggered only by the Owner and approved each time. Everything here runs against an
in-memory Etsy: no test reaches Etsy (AC-6.8.3).
"""

import json
import tomllib
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from htco_application.access.policy import Principal
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.contracts import ActionCatalog, ActionCatalogError
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRef,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.marketplace import (
    MarketplacePermissionError,
    MarketplaceShop,
    OrderReceipt,
    OrderShipment,
)
from htco_application.workforce.files import read_registry
from htco_application.workforce.registry import Registry, parse_automation, parse_employee
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from tests.application.workforce_fixtures import employee_document
from tests.automation_fakes import engine_document
from tests.execution_fakes import (
    REGISTRY,
    SCHEMAS,
    Approvals,
    Capabilities,
    MemoryActionStore,
    Principals,
    Shops,
    real_catalog,
)
from tests.log_capture import capture_logs
from tests.order_intake_fakes import paid_receipt

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("7c000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("7c300000-0000-0000-0000-000000000001")
OWNER = uuid.UUID("7c100000-0000-0000-0000-000000000001")
ADMIN = uuid.UUID("7c100000-0000-0000-0000-000000000002")
ETSY_SHOP = 55501
RECEIPT = 4000000001
TRACKING = "LX123456789IL"  # a made-up code to search for: it must never leave the channel
SHIPMENTS = "etsy.shipments.create"
SHOP_UPDATE = "etsy.shop.update"
WRITES = frozenset({SHIPMENTS, SHOP_UPDATE})
GRANTED = frozenset({"shops_r", "transactions_r", "listings_r", "transactions_w", "shops_w"})


@dataclass
class FakeEtsy:
    """Etsy without the network: one shop, receipts by ID, and a record of every call."""

    receipts: dict[int, OrderReceipt] = field(default_factory=dict)
    on_vacation: bool = False
    refuse: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def get_order_receipt(self, shop_id: int, receipt_id: int) -> OrderReceipt:
        self.calls.append(("get_order_receipt", {"shop_id": shop_id, "receipt_id": receipt_id}))
        return self.receipts[receipt_id]

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop:
        self.calls.append(("get_shop", {"shop_id": shop_id, "urgent": urgent}))
        return MarketplaceShop(
            shop_id=shop_id,
            name="FakeShop",
            currency_code="USD",
            url=None,
            is_vacation=self.on_vacation,
            active_listing_count=1,
            sold_transaction_count=1,
            created_at=NOW,
            updated_at=NOW,
        )

    async def create_receipt_shipment(
        self, shop_id: int, receipt_id: int, *, tracking_code: str, carrier_name: str
    ) -> None:
        self.calls.append(
            (
                "create_receipt_shipment",
                {"shop_id": shop_id, "receipt_id": receipt_id, "tracking_code": tracking_code,
                 "carrier_name": carrier_name},
            )
        )  # fmt: skip
        if self.refuse is not None:
            raise self.refuse
        receipt = self.receipts[receipt_id]
        shipment = OrderShipment(carrier=carrier_name, tracking_code=tracking_code, notified_at=NOW)
        self.receipts[receipt_id] = replace(
            receipt, is_shipped=True, shipments=(*receipt.shipments, shipment)
        )

    async def update_shop_vacation(
        self, shop_id: int, *, is_vacation: bool, vacation_message: str | None
    ) -> None:
        self.calls.append(
            (
                "update_shop_vacation",
                {"shop_id": shop_id, "is_vacation": is_vacation, "message": vacation_message},
            )
        )
        if self.refuse is not None:
            raise self.refuse
        self.on_vacation = is_vacation

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if call[0].startswith(("create_", "update_"))]


@dataclass
class EtsyShops:
    def etsy_shop_id(self, shop_id: uuid.UUID) -> int | None:
        return ETSY_SHOP if shop_id == SHOP else None


@dataclass
class Scopes:
    granted: dict[uuid.UUID, frozenset[str]] = field(default_factory=lambda: {SHOP: GRANTED})

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]:
        return self.granted.get(shop_id, frozenset())


def office() -> Registry:
    """The real tools, an employee and an automation that would like to write."""
    real = read_registry(REGISTRY).registry()
    employee = parse_employee(employee_document("eitan", allowed_tools=sorted(WRITES)))
    automation = parse_automation(
        engine_document("tracking-synchronization", allowed_actions=sorted(WRITES))
    )
    return Registry(
        employees=MappingProxyType({"eitan": employee}),
        tools=real.tools,
        automations=MappingProxyType({"tracking-synchronization": automation}),
        advisors=MappingProxyType({}),
    )


def principal(role: Role, *, reauthenticated: timedelta | None = timedelta(minutes=1)) -> Principal:
    return Principal(
        organization_id=ORG,
        role=role,
        assurance=AssuranceLevel.AAL2,
        reauthenticated_at=None if reauthenticated is None else NOW - reauthenticated,
        granted_shops=frozenset({SHOP}),
    )


class World:
    def __init__(
        self,
        *,
        flags: frozenset[str] = WRITES,
        scopes: frozenset[str] = GRANTED,
        owner: Principal | None = None,
    ) -> None:
        self.etsy = FakeEtsy(receipts={RECEIPT: paid_receipt(RECEIPT)})
        self.store = MemoryActionStore()
        self.approvals = Approvals()
        self.principals = Principals(
            {(OWNER, ORG): owner or principal(Role.OWNER), (ADMIN, ORG): principal(Role.ADMIN)}
        )
        self.registry = office()
        self.gateway = ActionGatewayService(
            GatewayParts(
                catalog=real_catalog(self.registry),
                registry=lambda: self.registry,
                store=self.store,
                principals=self.principals,
                shops=Shops({SHOP: ORG}),
                approvals=self.approvals,
                adapters={Channel.API: EtsyApiChannel(self.etsy, EtsyShops(), writes=self.etsy)},
                capabilities=Capabilities(),
                clock=lambda: NOW,
                scopes=Scopes({SHOP: scopes}),
                write_flags=flags,
            )
        )


def tracking(**changes: Any) -> ActionRequest:
    values: dict[str, Any] = {
        "action": ActionRef(SHIPMENTS, 1),
        "actor": ActorRef(ActorKind.USER, str(OWNER)),
        "organization_id": ORG,
        "idempotency_key": f"shipment:{RECEIPT}:{TRACKING}",
        "correlation_id": "corr-6-6",
        "input": {"receipt_id": RECEIPT, "tracking_code": TRACKING, "carrier_name": "israel-post"},
        "shop_id": SHOP,
        "owner_trigger": True,
    } | changes
    return ActionRequest(**values)


def vacation(on: bool = True, **changes: Any) -> ActionRequest:  # noqa: FBT001, FBT002 - the switch
    values: dict[str, Any] = {
        "action": ActionRef(SHOP_UPDATE, 1),
        "actor": ActorRef(ActorKind.USER, str(OWNER)),
        "organization_id": ORG,
        "idempotency_key": f"vacation:{SHOP}:{on}:1",
        "correlation_id": "corr-vacation",
        "input": {"is_vacation": on, "vacation_message": "Back on 2 November"} if on else {
            "is_vacation": False
        },
        "shop_id": SHOP,
        "owner_trigger": True,
    } | changes  # fmt: skip
    return ActionRequest(**values)


def denied(result: ActionResult) -> tuple[ActionOutcome, str | None]:
    return result.outcome, result.error_code


# --- The tracking upload (6.6) ---------------------------------------------------------------


@pytest.mark.ac("AC-6.8.1")
async def test_the_owners_trigger__uploads_the_tracking_once_and_verifies_it() -> None:
    world = World()

    result = await world.gateway.submit(tracking())
    again = await world.gateway.submit(tracking())

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert result.verified  # re-read: the code is on the receipt
    assert again == result  # the same key never sends twice
    assert [name for name, _ in world.etsy.writes()] == ["create_receipt_shipment"]
    assert world.etsy.writes()[0][1]["tracking_code"] == TRACKING
    assert result.output == {"receipt_id": RECEIPT, "sent": True, "already_present": False}


@pytest.mark.ac("AC-6.6.2")
async def test_a_tracking_code_already_on_the_receipt__sends_nothing_to_etsy() -> None:
    world = World()
    shipment = OrderShipment(carrier="israel-post", tracking_code=TRACKING, notified_at=NOW)
    world.etsy.receipts[RECEIPT] = replace(paid_receipt(RECEIPT), shipments=(shipment,))

    result = await world.gateway.submit(tracking())

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert result.output == {"receipt_id": RECEIPT, "sent": False, "already_present": True}
    assert world.etsy.writes() == []


async def test_the_tracking_code_never_leaves_the_channel() -> None:
    world = World()

    with capture_logs() as logs:
        result = await world.gateway.submit(tracking())

    steps = world.store.only().steps
    assert TRACKING not in repr(result)
    assert all(TRACKING not in repr((s.result, s.reason, s.audit, s.event)) for s in steps)
    assert TRACKING not in logs.raw()


@pytest.mark.ac("AC-6.8.1")
@pytest.mark.parametrize(
    ("actor", "trigger"),
    [
        (ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"), False),
        (ActorRef(ActorKind.EMPLOYEE, "eitan"), False),
        (ActorRef(ActorKind.USER, str(OWNER)), False),  # the Owner, but not by hand
        (ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"), True),
    ],
    ids=["automation", "employee", "owner-without-trigger", "automation-claiming-a-trigger"],
)
async def test_anything_but_the_owners_trigger__owner_trigger_required(
    actor: ActorRef, trigger: bool,  # noqa: FBT001
) -> None:  # fmt: skip
    world = World()

    result = await world.gateway.submit(tracking(actor=actor, owner_trigger=trigger))

    assert denied(result) == (ActionOutcome.DENIED, "owner_trigger_required")
    assert world.etsy.calls == []
    assert world.approvals.requested == []  # no approval to wait for: it never opens


async def test_an_admins_trigger__forbidden() -> None:
    world = World()

    result = await world.gateway.submit(tracking(actor=ActorRef(ActorKind.USER, str(ADMIN))))

    assert denied(result) == (ActionOutcome.DENIED, "forbidden")
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.6.3")
@pytest.mark.parametrize("since", [None, timedelta(minutes=11)], ids=["never", "stale"])
async def test_the_owner_without_a_recent_step_up__step_up_required(since: timedelta | None) -> None:
    world = World(owner=principal(Role.OWNER, reauthenticated=since))

    result = await world.gateway.submit(tracking())

    assert denied(result) == (ActionOutcome.DENIED, "step_up_required")
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.8.2")
async def test_without_transactions_w__scope_missing_before_any_etsy_call() -> None:
    world = World(scopes=GRANTED - {"transactions_w"})

    result = await world.gateway.submit(tracking())

    assert denied(result) == (ActionOutcome.DENIED, "scope_missing")
    assert world.etsy.calls == []
    assert "reconnect" in (world.store.only().steps[-1].reason or "")  # how to grant it


async def test_with_the_write_flag_off__flag_off_before_any_etsy_call() -> None:
    world = World(flags=frozenset({SHOP_UPDATE}))

    result = await world.gateway.submit(tracking())

    assert denied(result) == (ActionOutcome.DENIED, "flag_off")
    assert world.etsy.calls == []


async def test_etsys_refusal__is_recorded_once_and_never_resent() -> None:
    world = World()
    world.etsy.refuse = MarketplacePermissionError("Etsy refused createReceiptShipment", status=403)

    result = await world.gateway.submit(tracking())
    again = await world.gateway.submit(tracking())

    assert result.outcome is ActionOutcome.FAILED
    assert result.error_code is not None
    assert again == result
    assert len(world.etsy.writes()) == 1  # one attempt: a write is not retried blindly


# --- Vacation mode (7.12, pulled forward) ----------------------------------------------------


async def test_vacation_mode__turned_on_by_the_owner_and_verified() -> None:
    world = World()

    result = await world.gateway.submit(vacation(on=True))

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert result.verified
    assert world.etsy.on_vacation
    assert world.etsy.writes() == [
        (
            "update_shop_vacation",
            {"shop_id": ETSY_SHOP, "is_vacation": True, "message": "Back on 2 November"},
        )
    ]
    assert result.output == {"is_vacation": True, "sent": True}


async def test_vacation_mode__already_in_that_state__sends_nothing() -> None:
    world = World()
    world.etsy.on_vacation = True

    result = await world.gateway.submit(vacation(on=True))

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert result.output == {"is_vacation": True, "sent": False}
    assert world.etsy.writes() == []


@pytest.mark.parametrize(
    ("world", "code"),
    [
        (lambda: World(scopes=GRANTED - {"shops_w"}), "scope_missing"),
        (lambda: World(flags=frozenset({SHIPMENTS})), "flag_off"),
        (lambda: World(owner=principal(Role.OWNER, reauthenticated=None)), "step_up_required"),
    ],
    ids=["no-shops_w", "flag-off", "no-step-up"],
)
async def test_vacation_mode__refused_before_any_etsy_call(world: Any, code: str) -> None:
    built: World = world()

    result = await built.gateway.submit(vacation(on=True))

    assert denied(result) == (ActionOutcome.DENIED, code)
    assert built.etsy.calls == []


async def test_vacation_mode__only_the_owner_by_hand() -> None:
    world = World()

    result = await world.gateway.submit(
        vacation(actor=ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"))
    )

    assert denied(result) == (ActionOutcome.DENIED, "owner_trigger_required")
    assert world.etsy.calls == []


# --- Every write tool (ADR-0022 validation) --------------------------------------------------


def test_every_owner_trigger_only_write__needs_the_owners_step_up() -> None:
    catalog = real_catalog(office())
    owner_only = {name for name, d in catalog.actions.items() if d.owner_trigger_only}

    assert owner_only >= WRITES
    assert all(catalog.actions[name].approval_policy.is_write for name in owner_only)
    assert {
        name: catalog.actions[name].bindings[0].requires_scope for name in sorted(WRITES)
    } == {SHIPMENTS: "transactions_w", SHOP_UPDATE: "shops_w"}


def test_the_catalog_refuses_an_owner_trigger_only_write_without_a_step_up() -> None:
    family = tomllib.loads((REGISTRY / "actions" / "etsy_open_api.toml").read_text("utf-8"))
    rate_limits = family["rate_limits"]
    loosened = [
        {**d, "user_capability": "edit_product_facts"} if d["tool"] == SHIPMENTS else d
        for d in family["actions"]
    ]
    schemas = {path.name: _json(path) for path in SCHEMAS.glob("*.json")}

    with pytest.raises(ActionCatalogError, match="step-up"):
        ActionCatalog.from_documents(
            loosened, tools=office().tools, schemas=schemas, rate_limits=rate_limits
        )


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
