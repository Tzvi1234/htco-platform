"""The Owner's write actions console and his press (slices 6.6 part B and 6.8).

Every press runs through the real Action Gateway over the real action contracts, against a fake
Etsy: the refusals (capability, step-up, scope, flag, kill switch) send nothing, a code already on
the receipt sends nothing (AC-6.6.2), every run is an audited execution the console shows, and a
relay item closes on its verified upload (6.10). No test reaches Etsy (AC-6.8.3).
"""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from htco_application.access.authentication import NoMembershipError
from htco_application.execution.gateway import ActionOutcome
from htco_application.marketplace import OrderShipment
from htco_application.owner_writes import (
    OWNER_WRITES,
    TRACKING_UPLOAD,
    VACATION_MODE,
    OwnerOnlyError,
    OwnerWriteRun,
    SecondFactorRequiredError,
    ShopNotVisibleError,
    StepUpRequiredError,
    WriteRefusedError,
    etsy_carrier_name,
    owner_write_console,
    preview_owner_write,
    relay_upload_preview,
    run_owner_write,
    upload_relay_tracking,
)
from htco_application.relay import RelayItem, RelayItemMovedError, RelayItemNotVisibleError
from htco_domain.actors import ActorKind
from htco_domain.fulfillment import Carrier, TrackingFormatError
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.relay import ClosedReason, RelayState
from tests.log_capture import capture_logs
from tests.order_intake_fakes import paid_receipt
from tests.owner_write_fakes import NOW, ORG, RECEIPT, SHOP, World, caller

pytestmark = pytest.mark.anyio

CODE = "LX123456789IL"  # a made-up code to search for: never in a log, an audit or a summary
PASTE = {"receipt_id": RECEIPT, "carrier": "israel_post", "code": "lx-123456789-il"}


async def press(world: World, key: str = "press-1", **changes: object) -> OwnerWriteRun:
    values = {**PASTE, **changes}
    return await run_owner_write(
        caller(), world.services, TRACKING_UPLOAD, organization_id=None, shop_id=SHOP,
        values=values, press_key=key, correlation_id="corr-owner", now=NOW,
    )  # fmt: skip


def a_handed_off_item(world: World) -> RelayItem:
    item = RelayItem(
        id=uuid.uuid7(), organization_id=ORG, shop_id=SHOP, receipt_id=RECEIPT,
        state=RelayState.HANDED_OFF, paid_at=NOW - timedelta(days=3), opened_at=NOW,
        updated_at=NOW, carrier="israel_post", last4="89IL",
    )  # fmt: skip
    world.relay.items[item.id] = item
    world.relay.codes[item.id] = CODE
    return item


# --- The console -----------------------------------------------------------------------------


def test_the_console__lists_every_registered_write_off_by_default() -> None:
    world = World(flags=frozenset(), scopes=frozenset({"shops_r", "transactions_r"}))

    rows = owner_write_console(caller(), world.services, organization_id=None, shop_id=SHOP)

    assert [r.write.key for r in rows] == [w.key for w in OWNER_WRITES]
    tracking, vacation = rows
    assert (tracking.capability, tracking.scope) == ("confirm_shipment_write", "transactions_w")
    assert (vacation.capability, vacation.scope) == ("update_shop_settings", "shops_w")
    assert not any(r.flag_on or r.enabled for r in rows)
    assert all(r.reconsent_required and r.last_run is None for r in rows)


async def test_the_console__shows_the_last_run_and_its_result() -> None:
    world = World()
    run = await press(world)

    tracking, vacation = owner_write_console(
        caller(), world.services, organization_id=None, shop_id=SHOP
    )

    assert tracking.last_run is not None
    assert tracking.last_run.execution_id == run.result.execution_id
    assert (tracking.last_run.outcome, tracking.last_run.verified) == ("succeeded", True)
    assert tracking.last_run.output == {"receipt_id": RECEIPT, "sent": True,
                                        "already_present": False}  # fmt: skip
    assert vacation.last_run is None


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.VIEWER])
def test_the_console__is_the_owners_only(role: Role) -> None:
    with pytest.raises(OwnerOnlyError):
        owner_write_console(caller(role), World().services, organization_id=None, shop_id=SHOP)


def test_the_console__another_shop_is_not_visible() -> None:
    with pytest.raises(ShopNotVisibleError):
        owner_write_console(caller(), World().services, organization_id=None,
                            shop_id=uuid.uuid4())  # fmt: skip
    with pytest.raises(NoMembershipError):
        owner_write_console(caller(), World().services, organization_id=uuid.uuid4(),
                            shop_id=SHOP)  # fmt: skip


# --- The confirm sheet -----------------------------------------------------------------------


def test_the_preview__says_what_is_written_without_the_code_and_sends_nothing() -> None:
    world = World()

    preview = preview_owner_write(
        caller(), world.services, TRACKING_UPLOAD, organization_id=None, shop_id=SHOP,
        values=PASTE, now=NOW,
    )  # fmt: skip

    assert preview.summary == {"receipt_id": RECEIPT, "carrier": "israel_post", "last4": "89IL"}
    assert preview.can_run
    assert CODE not in repr(preview)
    assert world.etsy.calls == []
    assert world.store.rows == {}


@pytest.mark.ac("AC-6.8.2")
def test_the_preview__names_every_refusal_and_the_reconsent() -> None:
    world = World(flags=frozenset(), halted=True, scopes=frozenset({"transactions_r"}))
    stale = caller(reauthenticated=timedelta(minutes=30))

    preview = preview_owner_write(
        stale, world.services, TRACKING_UPLOAD, organization_id=None, shop_id=SHOP,
        values=PASTE, now=NOW,
    )  # fmt: skip

    assert preview.blocked_by == ("flag_off", "writes_halted", "scope_missing", "step_up_required")
    assert preview.state.reconsent_required
    assert not preview.can_run


@pytest.mark.ac("AC-6.10.4")
def test_the_preview__rejects_a_code_in_the_wrong_format() -> None:
    with pytest.raises(TrackingFormatError) as raised:
        preview_owner_write(
            caller(), World().services, TRACKING_UPLOAD, organization_id=None, shop_id=SHOP,
            values={**PASTE, "code": "12"}, now=NOW,
        )  # fmt: skip
    assert "12" not in str(raised.value)


# --- The press: refusals ---------------------------------------------------------------------


@pytest.mark.ac("AC-6.6.1")
@pytest.mark.ac("AC-6.8.2")
async def test_a_missing_scope__refuses_before_any_etsy_call_and_records_it() -> None:
    world = World(scopes=frozenset({"shops_r", "transactions_r"}))

    with pytest.raises(WriteRefusedError) as raised:
        await press(world)

    assert raised.value.code == "scope_missing"
    assert raised.value.state.reconsent_required
    assert world.etsy.calls == []
    [row] = world.store.rows.values()
    assert row.result is not None
    assert (row.result.outcome, row.result.error_code) == (ActionOutcome.DENIED, "scope_missing")


async def test_the_flag_off__refuses_and_sends_nothing() -> None:
    world = World(flags=frozenset())

    with pytest.raises(WriteRefusedError) as raised:
        await press(world)

    assert raised.value.code == "flag_off"
    assert world.etsy.calls == []


async def test_the_kill_switch__refuses_and_sends_nothing() -> None:
    world = World(halted=True)

    with pytest.raises(WriteRefusedError) as raised:
        await press(world)

    assert raised.value.code == "writes_halted"
    assert world.etsy.calls == []


async def test_the_kill_switch_turned_on_during_the_press__stops_the_send() -> None:
    """The gateway reads the switch again right before the send (sage, #126)."""
    world = World()
    reads: list[None] = []

    def halted_after_routing() -> bool:
        reads.append(None)
        return len(reads) > 1  # on when routed, pulled before the send

    world.halted = halted_after_routing

    with pytest.raises(WriteRefusedError) as raised:
        await press(world)

    assert raised.value.code == "writes_halted"
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.6.3")
async def test_no_recent_step_up__refused_before_the_gateway() -> None:
    world = World()

    with pytest.raises(StepUpRequiredError):
        await run_owner_write(
            caller(reauthenticated=timedelta(minutes=11)), world.services, TRACKING_UPLOAD,
            organization_id=None, shop_id=SHOP, values=PASTE, press_key="k",
            correlation_id="c", now=NOW,
        )  # fmt: skip

    assert world.store.rows == {}  # the key is not used up: the same press works after it
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.6.3")
async def test_no_second_factor__refused_before_the_gateway() -> None:
    world = World()

    with pytest.raises(SecondFactorRequiredError):
        await run_owner_write(
            caller(aal=AssuranceLevel.AAL1, reauthenticated=None), world.services,
            TRACKING_UPLOAD, organization_id=None, shop_id=SHOP, values=PASTE, press_key="k",
            correlation_id="c", now=NOW,
        )  # fmt: skip

    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.6.1")
@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATIONS, Role.DEVELOPER, Role.VIEWER])
async def test_anyone_but_the_owner__refused(role: Role) -> None:
    world = World()

    with pytest.raises(OwnerOnlyError):
        await run_owner_write(
            caller(role), world.services, TRACKING_UPLOAD, organization_id=None, shop_id=SHOP,
            values=PASTE, press_key="k", correlation_id="c", now=NOW,
        )  # fmt: skip

    assert world.etsy.calls == []


async def test_the_same_key_for_another_order__is_its_own_press_not_the_first_answer() -> None:
    """The gateway answers a finished key's result whatever is asked: the press key is hashed
    with the request, so another order is never answered with the first order's upload."""
    world = World()
    first = await press(world, receipt_id=RECEIPT)
    world.etsy.receipts[RECEIPT + 1] = paid_receipt(RECEIPT + 1)

    second = await press(world, receipt_id=RECEIPT + 1)

    assert second.result.execution_id != first.result.execution_id
    assert [c["receipt_id"] for _, c in world.etsy.writes()] == [RECEIPT, RECEIPT + 1]


# --- The press: results ----------------------------------------------------------------------


@pytest.mark.ac("AC-6.6.3")
async def test_the_press__uploads_once_verifies_and_is_audited_without_the_code() -> None:
    world = World()

    with capture_logs() as logs:
        run = await press(world)
        again = await press(world)

    assert run.result.outcome is ActionOutcome.SUCCEEDED
    assert run.result.verified
    assert again.result == run.result
    [(name, sent)] = world.etsy.writes()
    assert (name, sent["tracking_code"], sent["carrier_name"]) == (
        "create_receipt_shipment", CODE, "israel-post",
    )  # fmt: skip
    audits = world.audits()
    assert [a.action for a in audits] == ["action.succeeded"]
    [audit] = audits
    assert audit.actor.kind is ActorKind.USER
    assert audit.after is not None
    assert audit.after["action"] == "etsy.shipments.create"
    assert CODE not in repr(audits)
    assert CODE not in repr(run)
    assert "123456789" not in logs.raw()


@pytest.mark.ac("AC-6.6.2")
async def test_a_code_already_on_the_receipt__sends_nothing_and_answers_already_present() -> None:
    world = World()
    shipment = OrderShipment(carrier="israel-post", tracking_code=CODE, notified_at=NOW)
    world.etsy.receipts[RECEIPT] = replace(paid_receipt(RECEIPT), shipments=(shipment,))

    run = await press(world)

    assert run.result.outcome is ActionOutcome.SUCCEEDED
    assert run.result.output == {"receipt_id": RECEIPT, "sent": False,
                                 "already_present": True}  # fmt: skip
    assert world.etsy.writes() == []


async def test_etsys_error__is_the_result_shown_and_recorded() -> None:
    from htco_application.marketplace import MarketplacePermissionError  # noqa: PLC0415

    world = World()
    world.etsy.refuse = MarketplacePermissionError("forbidden by Etsy", status=403)

    run = await press(world)

    assert run.result.outcome is ActionOutcome.FAILED
    assert run.state.last_run is not None
    assert run.state.last_run.outcome == "failed"


async def test_vacation_mode__set_through_the_gateway() -> None:
    world = World()

    run = await run_owner_write(
        caller(), world.services, VACATION_MODE, organization_id=None, shop_id=SHOP,
        values={"is_vacation": True, "vacation_message": "Back on 2 November"},
        press_key="vacation-1", correlation_id="c", now=NOW,
    )  # fmt: skip

    assert run.result.outcome is ActionOutcome.SUCCEEDED
    assert world.etsy.on_vacation
    assert [n for n, _ in world.etsy.writes()] == ["update_shop_vacation"]


async def test_a_verified_upload__closes_the_orders_relay_item() -> None:
    world = World()
    item = a_handed_off_item(world)

    await press(world)

    assert world.relay.items[item.id].closed_reason is ClosedReason.UPLOADED
    assert item.id not in world.relay.codes


# --- The relay item's upload (6.10) ----------------------------------------------------------


async def upload(world: World, item: RelayItem, key: str = "relay-1") -> OwnerWriteRun:
    return await upload_relay_tracking(
        caller(), world.services, organization_id=None, item_id=item.id, press_key=key,
        correlation_id="corr-relay", now=NOW,
    )  # fmt: skip


@pytest.mark.ac("AC-6.10.1")
async def test_a_relay_item__uploads_its_pasted_code_once_and_closes() -> None:
    world = World()
    item = a_handed_off_item(world)

    run = await upload(world, item)

    assert run.result.verified
    assert run.relay_item is not None
    assert run.relay_item.closed_reason is ClosedReason.UPLOADED
    assert [a.action for a in world.relay.audits] == [
        "relay.code_read_for_upload", "relay.uploaded",
    ]  # fmt: skip
    assert CODE not in repr(world.relay.audits)
    uploaded = world.relay.audits[1].after
    assert uploaded is not None
    assert uploaded["action_execution_id"] == str(run.result.execution_id)
    with pytest.raises(RelayItemMovedError):
        await upload(world, item, key="relay-2")  # closed: never uploaded again
    assert len(world.etsy.writes()) == 1


@pytest.mark.ac("AC-6.10.2")
async def test_a_relay_code_already_on_etsy__closes_on_that_evidence_sending_nothing() -> None:
    world = World()
    item = a_handed_off_item(world)
    shipment = OrderShipment(carrier="israel-post", tracking_code=CODE, notified_at=NOW)
    world.etsy.receipts[RECEIPT] = replace(paid_receipt(RECEIPT), shipments=(shipment,))

    run = await upload(world, item)

    assert run.relay_item is not None
    assert run.relay_item.closed_reason is ClosedReason.TRACKING_ON_ETSY
    assert world.etsy.writes() == []


@pytest.mark.ac("AC-6.10.3")
async def test_a_refused_relay_upload__reads_no_code_and_leaves_the_item_handed_off() -> None:
    world = World(flags=frozenset())
    item = a_handed_off_item(world)

    with pytest.raises(WriteRefusedError):
        await upload(world, item)

    assert world.relay.audits == []  # the code was never read
    assert world.relay.items[item.id].state is RelayState.HANDED_OFF
    assert world.etsy.calls == []


@pytest.mark.ac("AC-6.10.3")
async def test_a_failed_relay_upload__leaves_the_item_with_the_person() -> None:
    from htco_application.marketplace import MarketplacePermissionError  # noqa: PLC0415

    world = World()
    world.etsy.refuse = MarketplacePermissionError("forbidden by Etsy", status=403)
    item = a_handed_off_item(world)

    run = await upload(world, item)

    assert run.result.outcome is ActionOutcome.FAILED
    assert run.relay_item is not None
    assert run.relay_item.state is RelayState.HANDED_OFF


async def test_a_relay_item__is_the_owners_only_and_needs_a_pasted_code() -> None:
    world = World()
    item = a_handed_off_item(world)
    waiting = replace(item, id=uuid.uuid7(), state=RelayState.AWAITING_TRACKING, carrier=None)
    world.relay.items[waiting.id] = waiting

    with pytest.raises(RelayItemNotVisibleError):
        await upload_relay_tracking(
            caller(Role.ADMIN), world.services, organization_id=None, item_id=item.id,
            press_key="k", correlation_id="c", now=NOW,
        )  # fmt: skip
    with pytest.raises(RelayItemMovedError):
        await upload(world, waiting)
    preview = relay_upload_preview(
        caller(), world.services, organization_id=None, item_id=item.id, now=NOW
    )
    assert preview.summary["last4"] == "89IL"
    assert CODE not in repr(preview)


def test_etsy_carrier_names__match_the_actions_input_schema() -> None:
    import re  # noqa: PLC0415

    assert all(re.fullmatch(r"[a-z0-9-]{2,64}", etsy_carrier_name(c)) for c in Carrier)
