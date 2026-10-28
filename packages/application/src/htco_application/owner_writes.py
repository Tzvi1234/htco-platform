"""The Owner's write actions console and his press (slices 6.6 part B and 6.8, ADR-0022).

The Owner runs each implemented Etsy write by hand, sees what it will write before he confirms,
and sees afterwards that it succeeded. Every press goes through the Action Gateway only: the
gateway checks the Owner's capability with its step-up, the shop's OAuth scope, the write flag and
the kill switch (again right before the send), re-reads Etsy before and after, and records the
run as an action execution with its audit record. This module adds what the gateway does not:

- a generic registry of owner-triggerable writes (`OWNER_WRITES`): each names its gateway action
  and how a typed request becomes the action's input and a summary that is safe to show;
- the console (`owner_write_console`): every registered write with its flag, the kill switch, the
  scope the shop's grant must hold and the last run with its result;
- the confirm sheet (`preview_owner_write`): what will be written to which shop and order, what
  would refuse it now, and whether the shop must be reconnected to grant the scope (AC-6.8.2);
- the press (`run_owner_write`, `upload_relay_tracking`): the Owner only, with a second factor and
  a recent step-up (AC-6.6.3), checked before the gateway so a refusal never uses up a press key.
  A relay item (6.10) is uploaded from its pasted code and closes on the verified result.

Nothing here reaches Etsy itself, and no buyer data leaves it: a tracking code is shown and
summarised as its carrier and last four characters, never logged, audited or stored by the press.
"""

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Outcome, Principal, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.execution.contracts import ActionCatalog, ActionDefinition
from htco_application.execution.gateway import (
    ActionGateway,
    ActionOutcome,
    ActionRef,
    ActionRequest,
    ActionResult,
)
from htco_application.execution.service import Principals
from htco_application.jobs import JsonValue
from htco_application.relay import RelayItem, RelayItemMovedError, RelayItemNotVisibleError
from htco_application.relay import RelayStore as RelayItems
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import Carrier, TrackingNumber
from htco_domain.identity import Role
from htco_domain.relay import ClosedReason, RelayState

# Where the web app starts the Etsy connect flow again, for a scope the grant lacks (AC-6.8.2).
RECONNECT_PATH = "/v1/shops/etsy/connect"
VACATION_MESSAGE_MAX = 500

# Why a press would be refused now, in the order the confirm sheet shows them.
FLAG_OFF = "flag_off"
WRITES_HALTED = "writes_halted"
SCOPE_MISSING = "scope_missing"
MFA_REQUIRED = "mfa_required"
STEP_UP_REQUIRED = "step_up_required"


class InvalidWriteInputError(ValueError):
    """The request is not a valid input for the write; the message is safe to show."""


@dataclass(frozen=True)
class Prepared:
    """A typed request made the write's gateway input, and the summary of what it writes."""

    input: Mapping[str, JsonValue] = field(repr=False)  # may hold buyer data (a tracking code)
    # Safe to show, log and store: identifiers, the carrier and the last four characters.
    summary: Mapping[str, JsonValue]
    receipt_id: int | None = None  # the Etsy order the write changes, if one


@dataclass(frozen=True)
class OwnerWrite:
    """One owner-triggerable write. Its capability and scope come from the action's contract
    (`registry/actions/`), never from here, so the console cannot drift from what the gateway
    checks. Later writes (more API writes, then browser recipes) plug in as further entries."""

    key: str  # stable, for the web app: "tracking_upload"
    action: str  # the gateway action: "etsy.shipments.create"
    version: int
    prepare: Callable[[Mapping[str, Any]], Prepared] = field(repr=False, compare=False)


def etsy_carrier_name(carrier: Carrier) -> str:
    """Etsy's `carrier_name` for our carrier: lower case with dashes (`israel_post` →
    `israel-post`), as the action's input schema asks."""
    return carrier.value.replace("_", "-")


def _prepare_tracking(values: Mapping[str, Any]) -> Prepared:
    receipt = values.get("receipt_id")
    if not isinstance(receipt, int) or isinstance(receipt, bool) or receipt < 1:
        raise InvalidWriteInputError("receipt_id must be a positive Etsy receipt ID")
    # Carrier and format before anything else (AC-6.10.4); TrackingFormatError names no code.
    tracking = TrackingNumber.parse(str(values.get("carrier", "")), str(values.get("code", "")))
    return Prepared(
        input={
            "receipt_id": receipt,
            "tracking_code": tracking.code,
            "carrier_name": etsy_carrier_name(tracking.carrier),
        },
        summary={"receipt_id": receipt, "carrier": tracking.carrier.value,
                 "last4": tracking.last4},
        receipt_id=receipt,
    )  # fmt: skip


def _prepare_vacation(values: Mapping[str, Any]) -> Prepared:
    on = values.get("is_vacation")
    if not isinstance(on, bool):
        raise InvalidWriteInputError("is_vacation must be true or false")
    message = values.get("vacation_message")
    too_long = isinstance(message, str) and len(message) > VACATION_MESSAGE_MAX
    if message is not None and (not isinstance(message, str) or too_long):
        raise InvalidWriteInputError("the vacation message is at most 500 characters")
    text = message.strip() if isinstance(message, str) and on else ""
    sent: dict[str, JsonValue] = {"is_vacation": on}
    if text:
        sent["vacation_message"] = text
    # The message is the Owner's own text for every buyer to read, never buyer data.
    return Prepared(input=sent, summary=dict(sent))


TRACKING_UPLOAD = OwnerWrite("tracking_upload", "etsy.shipments.create", 1, _prepare_tracking)
VACATION_MODE = OwnerWrite("vacation_mode", "etsy.shop.update", 1, _prepare_vacation)
OWNER_WRITES: tuple[OwnerWrite, ...] = (TRACKING_UPLOAD, VACATION_MODE)


@dataclass(frozen=True)
class WriteSwitches:
    """The write flags (`HTCO_ETSY_WRITE_ACTIONS`, every write off unless named) and the kill
    switch over all of them. The gateway reads `is_on` at every check, the last one right before
    the send, so turning a flag off or halting the writes stops a press already on its way."""

    flags: frozenset[str] = frozenset()
    halted: Callable[[], bool] = field(default=lambda: False, compare=False)

    def flag_on(self, action_name: str) -> bool:
        return action_name in self.flags

    def is_on(self, action_name: str) -> bool:
        return self.flag_on(action_name) and not self.halted()


@dataclass(frozen=True)
class LastRun:
    """The latest execution of a write on a shop: identifiers, states and codes only."""

    execution_id: uuid.UUID
    state: str
    outcome: str | None
    error_code: str | None
    verified: bool
    output: Mapping[str, JsonValue] | None  # the write's contract output (no buyer data)
    created_at: datetime
    finished_at: datetime | None


class OwnerWriteHistory(Protocol):
    """Synchronous (psycopg's pool): the API runs it in a worker thread."""

    def last_runs(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, actions: frozenset[str]
    ) -> Mapping[str, LastRun]: ...


class OwnerWriteShops(Protocol):
    """The shop's organization and the OAuth scopes its current Etsy grant holds."""

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None: ...

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]: ...


# The gateway for one press: its user principals are the caller's own (the API's auth context).
type GatewayFor = Callable[[Principals], ActionGateway]


@dataclass(frozen=True)
class OwnerWriteServices:
    catalog: ActionCatalog
    gateway_for: GatewayFor
    shops: OwnerWriteShops
    history: OwnerWriteHistory
    switches: WriteSwitches
    relay: RelayItems | None = None  # the AutoVA relay (6.10): uploads from a relay item
    writes: tuple[OwnerWrite, ...] = OWNER_WRITES

    def write(self, key: str) -> OwnerWrite:
        for found in self.writes:
            if found.key == key:
                return found
        raise LookupError(key)


@dataclass(frozen=True)
class OwnerWriteState:
    """One console row."""

    write: OwnerWrite
    capability: str
    scope: str | None  # the Etsy OAuth scope the shop's grant must hold
    flag_on: bool
    halted: bool
    scope_granted: bool
    last_run: LastRun | None

    @property
    def enabled(self) -> bool:
        return self.flag_on and not self.halted

    @property
    def reconsent_required(self) -> bool:
        return self.scope is not None and not self.scope_granted


@dataclass(frozen=True)
class OwnerWritePreview:
    state: OwnerWriteState
    shop_id: uuid.UUID
    summary: Mapping[str, JsonValue]
    blocked_by: tuple[str, ...]  # empty: confirming now runs the write

    @property
    def can_run(self) -> bool:
        return not self.blocked_by


@dataclass(frozen=True)
class OwnerWriteRun:
    """The gateway's result, shown as it is (AC-6.8: success or Etsy's error), the console row
    after it, and the relay item it closed or left open."""

    result: ActionResult
    state: OwnerWriteState
    relay_item: RelayItem | None = None


class OwnerOnlyError(PermissionError):
    """Only the Owner sees and runs the owner-triggered writes."""


class SecondFactorRequiredError(PermissionError):
    """A press needs a session signed in with a second factor (aal2)."""


class StepUpRequiredError(PermissionError):
    """A press needs a recent re-authentication (step-up)."""


class ShopNotVisibleError(LookupError):
    """No such shop for the caller: another organization's, or none."""


class WriteRefusedError(Exception):
    """The gateway (or the relay's check before it reads a code) refused the write: `code` is
    `flag_off`, `writes_halted` or `scope_missing`."""

    def __init__(self, code: str, state: OwnerWriteState) -> None:
        super().__init__(code)
        self.code = code
        self.state = state


class PressKeyReusedError(Exception):
    """The press key was used before for another request."""


class RelayUnavailableError(Exception):
    """The relay store is not set up on this server."""


@dataclass(frozen=True)
class _CallerPrincipals:
    """The gateway's `Principals` for one press: the caller's own memberships, nobody else's."""

    auth: AuthContext

    def principal(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> Principal | None:
        if user_id != self.auth.user_id:
            return None
        try:
            return self.auth.principal_for(organization_id)
        except NoMembershipError:
            return None


def _organization(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return organization


def _definition(services: OwnerWriteServices, write: OwnerWrite) -> ActionDefinition:
    definition = services.catalog.get(write.action, write.version)
    if definition is None or definition.user_capability is None:  # pragma: no cover - startup
        raise LookupError(f"no owner-triggerable action {write.action} v{write.version}")
    return definition


def _scope_of(definition: ActionDefinition) -> str | None:
    scopes = [b.requires_scope for b in definition.bindings if not b.disabled and b.requires_scope]
    return scopes[0] if scopes else None


def _owner(
    auth: AuthContext, services: OwnerWriteServices, organization_id: uuid.UUID,
    shop_id: uuid.UUID,
) -> Principal:  # fmt: skip
    """The Owner of the shop's organization; raises `NoMembershipError`, `ShopNotVisibleError`
    or `OwnerOnlyError`."""
    principal = auth.principal_for(organization_id)
    if services.shops.organization_of_shop(shop_id) != organization_id:
        raise ShopNotVisibleError("no such shop")
    if principal.role is not Role.OWNER:
        raise OwnerOnlyError("only the Owner runs the owner-triggered writes")
    return principal


def _press_refusal(
    principal: Principal, definition: ActionDefinition, shop_id: uuid.UUID, now: datetime
) -> str | None:
    """`mfa_required` or `step_up_required` while the Owner may not press yet."""
    assert definition.user_capability is not None  # noqa: S101 - checked by `_definition`
    shop = ShopRef(id=shop_id, organization_id=principal.organization_id)
    outcome = decide(principal, definition.user_capability, shop=shop, now=now).outcome
    if outcome is Outcome.MFA_REQUIRED:
        return MFA_REQUIRED
    if outcome is Outcome.STEP_UP_REQUIRED:
        return STEP_UP_REQUIRED
    if outcome is not Outcome.ALLOW:  # pragma: no cover - the Owner holds every write capability
        raise OwnerOnlyError("the role may not run this write")
    return None


def _state(
    services: OwnerWriteServices,
    write: OwnerWrite,
    *,
    scopes: frozenset[str],
    last: LastRun | None,
) -> OwnerWriteState:
    definition = _definition(services, write)
    scope = _scope_of(definition)
    assert definition.user_capability is not None  # noqa: S101 - checked by `_definition`
    return OwnerWriteState(
        write=write,
        capability=definition.user_capability.value,
        scope=scope,
        flag_on=services.switches.flag_on(write.action),
        halted=services.switches.halted(),
        scope_granted=scope is None or scope in scopes,
        last_run=last,
    )


def _states(
    services: OwnerWriteServices,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    writes: tuple[OwnerWrite, ...],
) -> tuple[OwnerWriteState, ...]:
    scopes = services.shops.scopes_of(shop_id)
    last = services.history.last_runs(organization_id, shop_id, frozenset(w.action for w in writes))
    return tuple(_state(services, w, scopes=scopes, last=last.get(w.action)) for w in writes)


def owner_write_console(
    auth: AuthContext,
    services: OwnerWriteServices,
    *,
    organization_id: uuid.UUID | None,
    shop_id: uuid.UUID,
) -> tuple[OwnerWriteState, ...]:
    """Every registered write of the shop with its flag, kill switch, scope and last run, for
    the Owner. Raises `OrganizationRequiredError`, `NoMembershipError`, `ShopNotVisibleError`
    and `OwnerOnlyError`."""
    organization = _organization(auth, organization_id)
    _owner(auth, services, organization, shop_id)
    return _states(services, organization, shop_id, services.writes)


def _blocked(state: OwnerWriteState, refusal: str | None) -> tuple[str, ...]:
    found = [
        code
        for code, applies in (
            (FLAG_OFF, not state.flag_on),
            (WRITES_HALTED, state.halted),
            (SCOPE_MISSING, state.reconsent_required),
        )
        if applies
    ]
    return (*found, refusal) if refusal is not None else tuple(found)


def preview_owner_write(  # noqa: PLR0913 - the caller, the write and its request
    auth: AuthContext,
    services: OwnerWriteServices,
    write: OwnerWrite,
    *,
    organization_id: uuid.UUID | None,
    shop_id: uuid.UUID,
    values: Mapping[str, Any],
    now: datetime,
) -> OwnerWritePreview:
    """The confirm sheet: what the press would write to which shop and order, and what would
    refuse it now (the flag, the kill switch, the scope, the second factor, the step-up). It
    validates the request (raises `InvalidWriteInputError`, `TrackingFormatError`) and calls
    nothing outside HTCO; the re-read of Etsy happens at the press."""
    organization = _organization(auth, organization_id)
    principal = _owner(auth, services, organization, shop_id)
    prepared = write.prepare(values)
    [state] = _states(services, organization, shop_id, (write,))
    refusal = _press_refusal(principal, _definition(services, write), shop_id, now)
    return OwnerWritePreview(state, shop_id, prepared.summary, _blocked(state, refusal))


def relay_upload_preview(
    auth: AuthContext,
    services: OwnerWriteServices,
    *,
    organization_id: uuid.UUID | None,
    item_id: uuid.UUID,
    now: datetime,
) -> OwnerWritePreview:
    """The confirm sheet of a relay item's upload: its order, carrier and last four. Raises
    `RelayItemNotVisibleError` for anyone but the Owner, `RelayItemMovedError` when no pasted
    code waits."""
    organization = _organization(auth, organization_id)
    item = _relay_item(auth, services, organization, item_id)
    principal = auth.principal_for(organization)
    [state] = _states(services, organization, item.shop_id, (TRACKING_UPLOAD,))
    refusal = _press_refusal(principal, _definition(services, TRACKING_UPLOAD), item.shop_id, now)
    summary: dict[str, JsonValue] = {
        "receipt_id": item.receipt_id, "carrier": item.carrier, "last4": item.last4,
        "relay_item_id": str(item.id),
    }  # fmt: skip
    return OwnerWritePreview(state, item.shop_id, summary, _blocked(state, refusal))


def _press_key(auth: AuthContext, write: OwnerWrite, shop_id: uuid.UUID, key: str) -> str:
    """The gateway's idempotency key of one press: the caller's key, hashed with who and where
    (never the input, which may hold a tracking code)."""
    digest = hashlib.sha256(f"{auth.user_id}:{shop_id}:{write.key}:{key}".encode()).hexdigest()
    return f"owner:{write.key}:{digest[:48]}"


def _check_press(
    principal: Principal, definition: ActionDefinition, shop_id: uuid.UUID, now: datetime
) -> None:
    """Before the gateway (AC-6.6.3): a press without the second factor or the step-up never
    reaches it, so the Owner confirms and presses again with the same key."""
    refusal = _press_refusal(principal, definition, shop_id, now)
    if refusal == MFA_REQUIRED:
        raise SecondFactorRequiredError("sign in with your second factor first")
    if refusal == STEP_UP_REQUIRED:
        raise StepUpRequiredError("confirm it is you first")


async def _submit(  # noqa: PLR0913 - the press and its request
    auth: AuthContext,
    services: OwnerWriteServices,
    write: OwnerWrite,
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    prepared: Prepared,
    press_key: str,
    correlation_id: str,
) -> ActionResult:
    request = ActionRequest(
        action=ActionRef(write.action, write.version),
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        organization_id=organization_id,
        idempotency_key=_press_key(auth, write, shop_id, press_key),
        correlation_id=correlation_id,
        input=prepared.input,
        shop_id=shop_id,
        owner_trigger=True,  # the Owner's own press: `_authorize_user` checks the role
    )
    result = await services.gateway_for(_CallerPrincipals(auth)).submit(request)
    if result.outcome is not ActionOutcome.DENIED:
        return result
    code = result.error_code
    if code == "idempotency_conflict":
        raise PressKeyReusedError("the press key was used for another request")
    if code in {"flag_off", "scope_missing"}:
        [state] = _states(services, organization_id, shop_id, (write,))
        raise WriteRefusedError(
            WRITES_HALTED if code == "flag_off" and state.halted else code, state
        )
    if code == "step_up_required":
        raise StepUpRequiredError("confirm it is you first")
    if code == "mfa_required":
        raise SecondFactorRequiredError("sign in with your second factor first")
    if code == "not_found":
        raise ShopNotVisibleError("no such shop")
    if code == "invalid_input":
        raise InvalidWriteInputError("the request is not valid for this write")
    if code == "forbidden":
        raise OwnerOnlyError("the gateway refused the press")
    return result  # any other refusal is shown as it is


def _succeeded(result: ActionResult) -> bool:
    return result.outcome is ActionOutcome.SUCCEEDED and result.verified


async def run_owner_write(  # noqa: PLR0913 - the caller, the write and its request
    auth: AuthContext,
    services: OwnerWriteServices,
    write: OwnerWrite,
    *,
    organization_id: uuid.UUID | None,
    shop_id: uuid.UUID,
    values: Mapping[str, Any],
    press_key: str,
    correlation_id: str,
    now: datetime,
) -> OwnerWriteRun:
    """The Owner's press through the gateway. The same key answers the first result again and
    sends nothing (a replayed press); a new key runs every check again. A verified upload closes
    the order's open relay item, if one (6.10). Raises the errors of `preview_owner_write`, then
    `SecondFactorRequiredError`, `StepUpRequiredError`, `WriteRefusedError` (403) and
    `PressKeyReusedError` (422)."""
    organization = _organization(auth, organization_id)
    principal = _owner(auth, services, organization, shop_id)
    prepared = write.prepare(values)
    _check_press(principal, _definition(services, write), shop_id, now)
    result = await _submit(
        auth, services, write, organization_id=organization, shop_id=shop_id,
        prepared=prepared, press_key=press_key, correlation_id=correlation_id,
    )  # fmt: skip
    if _succeeded(result) and prepared.receipt_id is not None and services.relay is not None:
        services.relay.close_order(
            shop_id, prepared.receipt_id, reason=_closed_reason(result), now=now
        )
    [state] = _states(services, organization, shop_id, (write,))
    return OwnerWriteRun(result, state)


def _closed_reason(result: ActionResult) -> ClosedReason:
    sent = result.output is not None and result.output.get("sent") is True
    return ClosedReason.UPLOADED if sent else ClosedReason.TRACKING_ON_ETSY


def _relay_item(
    auth: AuthContext, services: OwnerWriteServices, organization_id: uuid.UUID,
    item_id: uuid.UUID,
) -> RelayItem:  # fmt: skip
    """The relay item waiting for its upload, for the Owner only (anyone else: not visible)."""
    if services.relay is None:
        raise RelayUnavailableError("the relay is not set up")
    item = services.relay.item(organization_id, item_id)
    if item is None:
        raise RelayItemNotVisibleError("no such relay item")
    try:
        _owner(auth, services, organization_id, item.shop_id)
    except OwnerOnlyError, ShopNotVisibleError:
        raise RelayItemNotVisibleError("no such relay item") from None
    if item.state is not RelayState.HANDED_OFF or item.carrier is None:
        raise RelayItemMovedError(item.state)
    return item


def _relay_audit(
    item: RelayItem, auth: AuthContext, action: str, correlation_id: str,
    **extra: JsonValue,
) -> AuditRecord:  # fmt: skip
    """Never the code: the carrier, its last four characters and the order."""
    return AuditRecord.new(
        organization_id=item.organization_id,
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        action=action,
        entity_type="relay_item",
        entity_id=str(item.id),
        after={"carrier": item.carrier, "last4": item.last4, "receipt_id": item.receipt_id,
               **extra},
        correlation_id=correlation_id,
    )  # fmt: skip


async def upload_relay_tracking(  # noqa: PLR0913 - the caller, the item and the press
    auth: AuthContext,
    services: OwnerWriteServices,
    *,
    organization_id: uuid.UUID | None,
    item_id: uuid.UUID,
    press_key: str,
    correlation_id: str,
    now: datetime,
) -> OwnerWriteRun:
    """Upload a relay item's pasted code to its Etsy order (6.10 step 3, "API first"). The flag,
    the kill switch and the scope are checked before the code is read, so a refused upload reads
    no buyer data; the gateway checks them again. A verified result closes the item: `uploaded`,
    or `tracking_on_etsy` when the receipt already carried the code (AC-6.10.2, nothing sent). A
    refused or failed upload leaves the item handed off to the person with the copy button
    (AC-6.10.3: never silently)."""
    organization = _organization(auth, organization_id)
    item = _relay_item(auth, services, organization, item_id)
    assert services.relay is not None  # noqa: S101 - `_relay_item` checked it
    principal = auth.principal_for(organization)
    _check_press(principal, _definition(services, TRACKING_UPLOAD), item.shop_id, now)
    [state] = _states(services, organization, item.shop_id, (TRACKING_UPLOAD,))
    blocked = _blocked(state, None)
    if blocked:
        raise WriteRefusedError(blocked[0], state)
    code = services.relay.reveal_code(
        organization, item.id,
        audit=_relay_audit(item, auth, "relay.code_read_for_upload", correlation_id), now=now,
    )  # fmt: skip
    if code is None:
        raise RelayItemNotVisibleError("no such relay item")
    carrier = Carrier(item.carrier) if item.carrier is not None else Carrier.OTHER
    prepared = Prepared(
        input={"receipt_id": item.receipt_id, "tracking_code": code,
               "carrier_name": etsy_carrier_name(carrier)},
        summary={"receipt_id": item.receipt_id, "carrier": carrier.value, "last4": item.last4},
        receipt_id=item.receipt_id,
    )  # fmt: skip
    result = await _submit(
        auth, services, TRACKING_UPLOAD, organization_id=organization, shop_id=item.shop_id,
        prepared=prepared, press_key=press_key, correlation_id=correlation_id,
    )  # fmt: skip
    after: RelayItem | None = item
    if _succeeded(result):
        reason = _closed_reason(result)
        closed = services.relay.close(
            item.id, reason=reason, now=now,
            audit=_relay_audit(
                item, auth,
                "relay.uploaded" if reason is ClosedReason.UPLOADED
                else "relay.closed_on_etsy_tracking",
                correlation_id, action_execution_id=str(result.execution_id),
            ),
        )  # fmt: skip
        after = closed or services.relay.item(organization, item.id)
    [state] = _states(services, organization, item.shop_id, (TRACKING_UPLOAD,))
    return OwnerWriteRun(result, state, after)
