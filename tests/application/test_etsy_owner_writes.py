"""The owner's Etsy writes on the Action Gateway (slices 6.6 and 6.8, vacation mode from 7.12).

The owner's policy of 2026-10-26: the first Etsy writes are the tracking upload
(`etsy.shipments.create`, scope `transactions_w`) and vacation mode (`etsy.shop.update`, scope
`shops_w`), both W3. The Owner's own press runs after a step-up; an agent's or an automation's
request waits for the Owner's approval, each time, and never runs before it. Every write flag is
off by default and checked again when the write runs. Everything here runs against an in-memory
Etsy: no test reaches Etsy (AC-6.8.3).
"""

# ruff: noqa: ARG002 - the fake Etsy keeps the read port's signatures it never reads

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
    HolidayPreference,
    MarketplacePermissionError,
    MarketplaceShop,
    OrderReceipt,
    OrderShipment,
    Page,
    Receipt,
    ShopReview,
)
from htco_application.workforce.files import read_registry
from htco_application.workforce.registry import Registry, parse_automation, parse_employee
from htco_domain.actions import ApprovalPolicy
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.approvals import decider_roles
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.workforce import Readiness
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
    # Etsy answers 200 to the POST but the shipment never appears on a re-read.
    loses_the_shipment: bool = False
    holidays: tuple[HolidayPreference, ...] = (
        HolidayPreference(holiday_id=7, country_iso="US", is_working=True, name="Thanksgiving"),
        HolidayPreference(holiday_id=9, country_iso="US", is_working=False, name="Christmas"),
    )
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
        if self.loses_the_shipment:
            return
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

    async def update_receipt_status(
        self, shop_id: int, receipt_id: int, *, was_shipped: bool | None, was_paid: bool | None
    ) -> None:
        self.calls.append(
            ("update_receipt_status", {"shop_id": shop_id, "receipt_id": receipt_id,
                                       "was_shipped": was_shipped, "was_paid": was_paid})
        )  # fmt: skip
        if self.refuse is not None:
            raise self.refuse
        if was_shipped:
            self.receipts[receipt_id] = replace(self.receipts[receipt_id], is_shipped=True)

    async def update_holiday_preference(
        self, shop_id: int, holiday_id: int, *, is_working: bool
    ) -> None:
        self.calls.append(
            ("update_holiday_preference",
             {"shop_id": shop_id, "holiday_id": holiday_id, "is_working": is_working})
        )  # fmt: skip
        if self.refuse is not None:
            raise self.refuse
        self.holidays = tuple(
            replace(h, is_working=is_working) if h.holiday_id == holiday_id else h
            for h in self.holidays
        )

    async def list_holiday_preferences(self, shop_id: int) -> tuple[HolidayPreference, ...]:
        self.calls.append(("list_holiday_preferences", {"shop_id": shop_id}))
        return self.holidays

    # The rest of the read port: these writes never use them.
    async def list_receipts(  # noqa: PLR0913 - the port's signature
        self,
        shop_id: int,
        *,
        min_last_modified: datetime | None = None,
        max_last_modified: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Receipt]:
        raise AssertionError("not read by the writes")

    async def get_receipt(self, shop_id: int, receipt_id: int) -> Receipt:
        raise AssertionError("not read by the writes")

    async def list_order_receipts(  # noqa: PLR0913 - the port's signature
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        was_paid: bool | None = None,
        was_shipped: bool | None = None,
    ) -> Page[OrderReceipt]:
        raise AssertionError("not read by the writes")

    async def list_reviews(
        self,
        shop_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
        min_created: datetime | None = None,
    ) -> Page[ShopReview]:
        raise AssertionError("not read by the writes")

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
    # The employee may use the two tools (made active here); its call still waits for the Owner.
    tools = {
        **real.tools,
        **{n: replace(real.tools[n], readiness=Readiness.ACTIVE) for n in WRITES},
    }
    return Registry(
        employees=MappingProxyType({"eitan": employee}),
        tools=MappingProxyType(tools),
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


@dataclass
class Switch:
    """The owner's kill switch, changed while a request waits."""

    on: set[str] = field(default_factory=lambda: set(WRITES))

    def is_on(self, action_name: str) -> bool:
        return action_name in self.on


class World:
    def __init__(
        self,
        *,
        flags: frozenset[str] | Switch = WRITES,
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


def vacation(on: bool = True, **changes: Any) -> ActionRequest:
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


@pytest.mark.ac("AC-6.6.2")
@pytest.mark.parametrize("pressed", ["LX-123456789-IL", "lx 123456789 il", "LX123456789IL"])
async def test_the_same_code_written_differently__is_already_present(pressed: str) -> None:
    """Sage on #161: dashes, spaces and case don't make a second shipment (and a second email
    to the buyer): the code is compared as the shared normaliser reads it."""
    world = World()
    shipment = OrderShipment(carrier="israel-post", tracking_code=TRACKING, notified_at=NOW)
    world.etsy.receipts[RECEIPT] = replace(paid_receipt(RECEIPT), shipments=(shipment,))
    request = tracking(input={"receipt_id": RECEIPT, "tracking_code": pressed,
                              "carrier_name": "israel-post"})  # fmt: skip

    result = await world.gateway.submit(replace(request, idempotency_key=f"shipment:{pressed}"))

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


REQUESTERS = [
    (ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"), False),
    (ActorRef(ActorKind.EMPLOYEE, "eitan"), False),
    (ActorRef(ActorKind.USER, str(OWNER)), False),  # the Owner, but not by hand
    (ActorRef(ActorKind.USER, str(ADMIN)), False),  # an Admin asks; the Owner decides
    (ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"), True),  # a claimed trigger
]
REQUESTER_IDS = ["automation", "employee", "owner-without-trigger", "admin", "automation-trigger"]


@pytest.mark.ac("AC-6.8.1")
@pytest.mark.parametrize(("actor", "trigger"), REQUESTERS, ids=REQUESTER_IDS)
async def test_anything_but_the_owners_press__waits_for_the_owners_approval_and_sends_nothing(
    actor: ActorRef, trigger: bool,
) -> None:  # fmt: skip
    world = World()

    first = await world.gateway.submit(tracking(actor=actor, owner_trigger=trigger))
    again = await world.gateway.submit(tracking(actor=actor, owner_trigger=trigger))

    assert first.outcome is ActionOutcome.PENDING_APPROVAL
    assert again.outcome is ActionOutcome.PENDING_APPROVAL
    assert len(world.approvals.requested) == 1  # one approval, however often it asks
    assert world.etsy.calls == []


@pytest.mark.parametrize(("actor", "trigger"), REQUESTERS[:2], ids=REQUESTER_IDS[:2])
async def test_an_agents_request__runs_once_after_the_owners_approval(
    actor: ActorRef, trigger: bool,
) -> None:  # fmt: skip
    world = World()
    request = tracking(actor=actor, owner_trigger=trigger)
    await world.gateway.submit(request)
    approval = world.approvals.approve_last()

    done = await world.gateway.submit(replace(request, approval_id=approval))
    again = await world.gateway.submit(replace(request, approval_id=approval))

    assert done.outcome is ActionOutcome.SUCCEEDED
    assert done.verified
    assert again == done
    assert [name for name, _ in world.etsy.writes()] == ["create_receipt_shipment"]


def test_only_the_owner_decides_these_writes() -> None:
    """W3: an Admin cannot approve them, whoever asked (PERMISSION_MATRIX §1a)."""
    catalog = real_catalog(office())

    for name in WRITES:
        policy = catalog.actions[name].approval_policy
        assert policy is ApprovalPolicy.W3
        assert decider_roles(policy) == {Role.OWNER}


@pytest.mark.ac("AC-6.6.3")
async def test_the_kill_switch__stops_an_approved_write_that_waited() -> None:
    """Sage, #126: the flag is read again when the write runs, not only when it was asked."""
    switch = Switch()
    world = World(flags=switch)
    request = tracking(actor=ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"))
    await world.gateway.submit(request)
    approval = world.approvals.approve_last()
    switch.on.discard(SHIPMENTS)  # the owner pulls the switch while it waits

    result = await world.gateway.submit(replace(request, approval_id=approval))

    assert denied(result) == (ActionOutcome.DENIED, "flag_off")
    assert world.etsy.writes() == []


async def test_a_draft_automations_queued_write__stops_when_it_runs() -> None:
    """Sage, #126: a run queued before its automation went back to draft reaches the gateway
    again when it executes, and its write stops."""
    world = World()
    automation = world.registry.automations["tracking-synchronization"]
    world.registry = replace(
        world.registry,
        automations=MappingProxyType(
            {"tracking-synchronization": replace(automation, readiness=Readiness.DRAFT)}
        ),
    )

    result = await world.gateway.submit(
        tracking(actor=ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"))
    )

    assert denied(result) == (ActionOutcome.DENIED, "automation_not_ready")
    assert world.etsy.calls == []
    assert world.approvals.requested == []


async def test_an_admins_trigger__forbidden() -> None:
    world = World()

    result = await world.gateway.submit(tracking(actor=ActorRef(ActorKind.USER, str(ADMIN))))

    assert denied(result) == (ActionOutcome.DENIED, "forbidden")
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.6.3")
@pytest.mark.parametrize("since", [None, timedelta(minutes=11)], ids=["never", "stale"])
async def test_the_owner_without_a_recent_step_up__step_up_required(
    since: timedelta | None,
) -> None:
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


@dataclass
class FlipsAtTheSend:
    """On when the action is routed, off by the time it is sent (the owner pulled the switch
    while it waited for its slot)."""

    asked: int = 0

    def is_on(self, action_name: str) -> bool:
        self.asked += 1
        return self.asked == 1


async def test_the_kill_switch__is_read_again_right_before_the_send() -> None:
    """Quill on #161 (mutant G4): the second check, not the router's, stops it."""
    switch = FlipsAtTheSend()
    world = World(flags=switch)  # type: ignore[arg-type]  # a WriteSwitch

    result = await world.gateway.submit(tracking())

    assert switch.asked == 2  # routed, then checked again before the send
    assert denied(result) == (ActionOutcome.DENIED, "flag_off")
    assert world.etsy.writes() == []


@pytest.mark.ac("AC-6.6.2")
async def test_a_write_the_re_read_cannot_find__is_not_verified_and_never_resent() -> None:
    """Quill on #161 (mutant G12): Etsy accepts the POST, but the re-read lacks the code."""
    world = World()
    world.etsy.loses_the_shipment = True

    result = await world.gateway.submit(tracking())
    again = await world.gateway.submit(tracking())

    assert result.outcome is ActionOutcome.FAILED
    assert not result.verified
    assert again == result  # final for its key: nothing is resent
    assert len(world.etsy.writes()) == 1


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


async def test_vacation_mode__an_agents_request_waits_for_the_owner() -> None:
    world = World()

    result = await world.gateway.submit(
        vacation(actor=ActorRef(ActorKind.AUTOMATION, "tracking-synchronization"))
    )

    assert result.outcome is ActionOutcome.PENDING_APPROVAL
    assert world.etsy.calls == []


# --- Every write tool (ADR-0022 validation) --------------------------------------------------


def test_both_writes__need_their_scope() -> None:
    catalog = real_catalog(office())

    assert {name: catalog.actions[name].bindings[0].requires_scope for name in sorted(WRITES)} == {
        SHIPMENTS: "transactions_w",
        SHOP_UPDATE: "shops_w",
    }


def test_the_catalog_refuses_a_w3_write_without_the_owners_step_up() -> None:
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
