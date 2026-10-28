"""The Owner's write actions console and his press (slices 6.6 part B and 6.8, ADR-0022).

- `GET /v1/shops/{shop_id}/owner-writes`: every owner-triggerable write of the shop with its flag,
  the kill switch, the scope the shop's Etsy grant must hold and the last run with its result.
- `POST /v1/shops/{shop_id}/owner-writes/{tracking-upload,vacation-mode}/preview`: the confirm
  sheet: what will be written to which shop and order, and what would refuse it now, with the
  reconnect link when the grant lacks the scope (AC-6.8.2). Nothing is sent anywhere.
- `POST /v1/shops/{shop_id}/owner-writes/{tracking-upload,vacation-mode}`: the press, through the
  Action Gateway only, with an `Idempotency-Key` (a replayed press answers the first result).
- `GET /v1/relay-items/{item_id}/upload-preview`, `POST /v1/relay-items/{item_id}/upload`: the
  AutoVA relay's upload of a pasted code (6.10); a verified result closes the item.

The Owner only; a press needs a second factor and a recent step-up (AC-6.6.3). Without the
capability, the scope, the flag or with the kill switch on, a press answers 403 and sends nothing
(AC-6.6.1, AC-6.8.2). A tracking code already on the receipt is never sent again and answers
`already_present` (AC-6.6.2). Every run is an action execution with its audit record. Responses
carry identifiers, states and codes only; a tracking code only as its last four characters.
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from htco_api.auth import CurrentAuth
from htco_api.idempotency import IDEMPOTENCY_KEY_REUSED
from htco_api.identity import ORGANIZATION_REQUIRED, problem_responses
from htco_api.incidents import RELAY_ITEM_MOVED, RELAY_UNAVAILABLE, RelayItemView
from htco_api.policy_guard import FORBIDDEN, MFA_REQUIRED, STEP_UP_REQUIRED
from htco_api.problems import NOT_FOUND, Problem, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.owner_writes import (
    RECONNECT_PATH,
    TRACKING_UPLOAD,
    VACATION_MODE,
    InvalidWriteInputError,
    OwnerOnlyError,
    OwnerWrite,
    OwnerWritePreview,
    OwnerWriteRun,
    OwnerWriteServices,
    OwnerWriteState,
    PressKeyReusedError,
    RelayUnavailableError,
    SecondFactorRequiredError,
    ShopNotVisibleError,
    StepUpRequiredError,
    WriteRefusedError,
    owner_write_console,
    preview_owner_write,
    relay_upload_preview,
    run_owner_write,
    upload_relay_tracking,
)
from htco_application.relay import RelayItem, RelayItemMovedError, RelayItemNotVisibleError
from htco_application.shops import OrganizationRequiredError
from htco_domain.fulfillment import TrackingFormatError

OWNER_WRITES_UNAVAILABLE = ProblemType(
    "owner-writes-unavailable", "Write actions not available", 503,
    "The owner's write actions are not set up on this server.",
)  # fmt: skip
WRITE_FLAG_OFF = ProblemType(
    "write-flag-off", "Write switched off", 403,
    "This write is switched off. Nothing was sent to Etsy.",
)  # fmt: skip
WRITES_HALTED = ProblemType(
    "writes-halted", "Writes halted", 403,
    "Every Etsy write is halted by the kill switch. Nothing was sent to Etsy.",
)  # fmt: skip
SCOPE_MISSING = ProblemType(
    "scope-missing", "Reconnect to grant", 403,
    "The shop's Etsy connection does not allow this write. Reconnect the shop to grant it.",
)  # fmt: skip

_REFUSALS = {"flag_off": WRITE_FLAG_OFF, "writes_halted": WRITES_HALTED}


class LastRunView(BaseModel):
    execution_id: uuid.UUID
    state: str
    outcome: str | None
    error_code: str | None
    verified: bool
    # The write's contract output, for example {"receipt_id", "sent", "already_present"}.
    output: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class OwnerWriteStateView(BaseModel):
    key: Literal["tracking_upload", "vacation_mode"]
    action: str
    version: int
    capability: str
    # The Etsy OAuth scope the shop's grant must hold; null when none.
    required_scope: str | None
    flag_on: bool
    writes_halted: bool
    # The flag is on and the kill switch is off.
    enabled: bool
    scope_granted: bool
    reconsent_required: bool
    # Where the web app reconnects the shop to grant the scope (AC-6.8.2); null when granted.
    reconnect_url: str | None
    last_run: LastRunView | None


class OwnerWriteListView(BaseModel):
    shop_id: uuid.UUID
    items: list[OwnerWriteStateView]


class OwnerWritePreviewView(BaseModel):
    write: OwnerWriteStateView
    shop_id: uuid.UUID
    # What will be written: identifiers, the carrier and the last four characters of a code.
    target: dict[str, Any]
    # Why a press would be refused now: flag_off, writes_halted, scope_missing, mfa_required,
    # step_up_required. Empty: confirming runs the write.
    blocked_by: list[str]
    can_run: bool


class OwnerWriteRunView(BaseModel):
    """The gateway's result as it is: Etsy's error is shown too (`outcome` failed)."""

    execution_id: uuid.UUID
    outcome: Literal[
        "succeeded", "failed", "denied", "pending_approval", "handed_to_human", "deferred"
    ]
    verified: bool
    error_code: str | None
    # True when this press sent the write; false when Etsy already had it (AC-6.6.2).
    sent: bool | None
    already_present: bool | None
    retry_at: datetime | None
    write: OwnerWriteStateView
    relay_item: RelayItemView | None = None


class TrackingUpload(BaseModel):
    receipt_id: int = Field(ge=1)
    carrier: str = Field(min_length=1, max_length=20)
    # Spaces and dashes as the supplier shows them are fine; the number is validated.
    code: str = Field(min_length=1, max_length=60)


class VacationMode(BaseModel):
    is_vacation: bool
    # Sent only when turning vacation mode on.
    vacation_message: str | None = Field(default=None, max_length=500)


def _services(request: Request) -> OwnerWriteServices:
    found = getattr(request.app.state, "owner_writes", None)
    if not isinstance(found, OwnerWriteServices):
        raise ProblemError(OWNER_WRITES_UNAVAILABLE)
    return found


def _state_view(state: OwnerWriteState) -> OwnerWriteStateView:
    last = state.last_run
    return OwnerWriteStateView(
        key=state.write.key,  # type: ignore[arg-type]
        action=state.write.action, version=state.write.version, capability=state.capability,
        required_scope=state.scope, flag_on=state.flag_on, writes_halted=state.halted,
        enabled=state.enabled, scope_granted=state.scope_granted,
        reconsent_required=state.reconsent_required,
        reconnect_url=RECONNECT_PATH if state.reconsent_required else None,
        last_run=None if last is None else LastRunView(
            execution_id=last.execution_id, state=last.state, outcome=last.outcome,
            error_code=last.error_code, verified=last.verified,
            output=None if last.output is None else dict(last.output),
            created_at=last.created_at, finished_at=last.finished_at,
        ),
    )  # fmt: skip


def _relay_view(item: RelayItem) -> RelayItemView:
    return RelayItemView(
        id=item.id, shop_id=item.shop_id, receipt_id=item.receipt_id, state=item.state.value,
        closed_reason=None if item.closed_reason is None else item.closed_reason.value,
        paid_at=item.paid_at, opened_at=item.opened_at, updated_at=item.updated_at,
    )  # fmt: skip


def _preview_view(preview: OwnerWritePreview) -> OwnerWritePreviewView:
    return OwnerWritePreviewView(
        write=_state_view(preview.state), shop_id=preview.shop_id, target=dict(preview.summary),
        blocked_by=list(preview.blocked_by), can_run=preview.can_run,
    )  # fmt: skip


def _flag(output: Mapping[str, Any] | None, name: str) -> bool | None:
    value = None if output is None else output.get(name)
    return value if isinstance(value, bool) else None


def _run_view(run: OwnerWriteRun) -> OwnerWriteRunView:
    result = run.result
    return OwnerWriteRunView(
        execution_id=result.execution_id, outcome=result.outcome.value,
        verified=result.verified, error_code=result.error_code,
        sent=_flag(result.output, "sent"),
        already_present=_flag(result.output, "already_present"),
        retry_at=result.retry_at, write=_state_view(run.state),
        relay_item=None if run.relay_item is None else _relay_view(run.relay_item),
    )  # fmt: skip


def _problem(error: Exception) -> ProblemError:  # noqa: PLR0911 - one answer per refusal
    if isinstance(error, OrganizationRequiredError):
        return ProblemError(ORGANIZATION_REQUIRED)
    if isinstance(error, NoMembershipError | ShopNotVisibleError | RelayItemNotVisibleError):
        return ProblemError(NOT_FOUND)
    if isinstance(error, SecondFactorRequiredError):
        return ProblemError(MFA_REQUIRED)
    if isinstance(error, StepUpRequiredError):
        return ProblemError(STEP_UP_REQUIRED)
    if isinstance(error, OwnerOnlyError):
        return ProblemError(FORBIDDEN)
    if isinstance(error, WriteRefusedError):
        if error.code == "scope_missing":
            return ProblemError(
                SCOPE_MISSING,
                extra={"scope": error.state.scope or "", "reconnect_url": RECONNECT_PATH},
            )
        return ProblemError(_REFUSALS[error.code])
    if isinstance(error, PressKeyReusedError):
        return ProblemError(IDEMPOTENCY_KEY_REUSED)
    if isinstance(error, TrackingFormatError | InvalidWriteInputError):
        # The message is safe to show: it names the carrier or the field, never the code.
        return ProblemError(ProblemType("write-input-invalid", "Invalid input", 422, str(error)))
    if isinstance(error, RelayItemMovedError):
        return ProblemError(RELAY_ITEM_MOVED)
    if isinstance(error, RelayUnavailableError):
        return ProblemError(RELAY_UNAVAILABLE)
    raise error


_KNOWN = (
    OrganizationRequiredError, NoMembershipError, ShopNotVisibleError, RelayItemNotVisibleError,
    SecondFactorRequiredError, StepUpRequiredError, OwnerOnlyError, WriteRefusedError,
    PressKeyReusedError, TrackingFormatError, InvalidWriteInputError, RelayItemMovedError,
    RelayUnavailableError,
)  # fmt: skip
_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    400: "The caller belongs to several organizations and named none",
    404: "No such shop for the caller",
    503: "The write actions are not set up on this server (owner-writes-unavailable)",
}
_PREVIEW = {
    **_COMMON,
    403: "Not the Owner (forbidden)",
    422: ("An invalid ID or input (write-input-invalid)", ValidationProblem),
}
_PRESS = {
    **_COMMON,
    403: "Not the Owner (forbidden), no second factor (mfa-required), no recent step-up "
    "(step-up-required), the write switched off (write-flag-off), the kill switch on "
    "(writes-halted), or the grant lacks the scope (scope-missing, with `reconnect_url`)",
    422: (
        "An invalid ID or input (write-input-invalid), or the Idempotency-Key was used for "
        "another request (idempotency-key-reused)",
        ValidationProblem,
    ),
}
PressKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]


def owner_writes_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["owner-writes"])
    Services = Annotated[OwnerWriteServices, Depends(_services)]  # noqa: N806 - a type alias

    def _correlation(request: Request) -> str:
        return str(getattr(request.state, "request_id", None) or uuid.uuid7())

    async def _preview(  # noqa: PLR0913, PLR0917 - the caller, the write and its request
        auth: AuthContext, services: OwnerWriteServices, write: OwnerWrite,
        organization_id: uuid.UUID | None, shop_id: uuid.UUID, values: dict[str, Any],
    ) -> OwnerWritePreviewView:  # fmt: skip
        try:
            preview = await anyio.to_thread.run_sync(
                lambda: preview_owner_write(
                    auth, services, write, organization_id=organization_id, shop_id=shop_id,
                    values=values, now=clock(),
                )
            )  # fmt: skip
        except _KNOWN as error:
            raise _problem(error) from None
        return _preview_view(preview)

    async def _press(  # noqa: PLR0913, PLR0917 - the caller, the write and the press
        request: Request, auth: AuthContext, services: OwnerWriteServices, write: OwnerWrite,
        organization_id: uuid.UUID | None, shop_id: uuid.UUID, values: dict[str, Any],
        press_key: str,
    ) -> OwnerWriteRunView:  # fmt: skip
        try:
            run = await run_owner_write(
                auth, services, write, organization_id=organization_id, shop_id=shop_id,
                values=values, press_key=press_key, correlation_id=_correlation(request),
                now=clock(),
            )  # fmt: skip
        except _KNOWN as error:
            raise _problem(error) from None
        return _run_view(run)

    @router.get(
        "/shops/{shop_id}/owner-writes",
        summary="The shop's owner-triggered writes with their switches and last run",
        responses=problem_responses(
            {**_COMMON, 403: "Not the Owner (forbidden)", 422: ("Invalid ID", ValidationProblem)}
        ),
    )
    async def list_owner_writes(
        shop_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWriteListView:
        """The write actions console (the Owner only): each write's flag, the kill switch, the
        scope it needs and whether the grant holds it, and its last run with the result."""
        try:
            states = await anyio.to_thread.run_sync(
                lambda: owner_write_console(
                    auth, services, organization_id=organization_id, shop_id=shop_id
                )
            )
        except _KNOWN as error:
            raise _problem(error) from None
        response.headers["Cache-Control"] = "no-store"
        return OwnerWriteListView(shop_id=shop_id, items=[_state_view(s) for s in states])

    @router.post(
        "/shops/{shop_id}/owner-writes/tracking-upload/preview",
        summary="What a tracking upload would write, and what would refuse it now",
        responses=problem_responses(_PREVIEW),
    )
    async def preview_tracking_upload(  # noqa: PLR0913, PLR0917 - the route's inputs
        shop_id: uuid.UUID,
        body: TrackingUpload,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWritePreviewView:
        """The confirm sheet; validates the carrier and format (AC-6.10.4); sends nothing."""
        preview = await _preview(
            auth, services, TRACKING_UPLOAD, organization_id, shop_id, body.model_dump()
        )
        response.headers["Cache-Control"] = "no-store"
        return preview

    @router.post(
        "/shops/{shop_id}/owner-writes/tracking-upload",
        summary="Upload a tracking number to one Etsy order (the Owner's press)",
        responses=problem_responses(_PRESS),
    )
    async def run_tracking_upload(  # noqa: PLR0913, PLR0917 - the route's inputs
        shop_id: uuid.UUID,
        body: TrackingUpload,
        request: Request,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        idempotency_key: PressKey,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWriteRunView:
        """`etsy.shipments.create` through the Action Gateway: re-reads the receipt and sends
        nothing when the code is there (`already_present`), else sends once and re-reads. The
        same Idempotency-Key answers the first result again; a refused press needs a new key."""
        run = await _press(
            request, auth, services, TRACKING_UPLOAD, organization_id, shop_id,
            body.model_dump(), idempotency_key,
        )  # fmt: skip
        response.headers["Cache-Control"] = "no-store"
        return run

    @router.post(
        "/shops/{shop_id}/owner-writes/vacation-mode/preview",
        summary="What setting vacation mode would write, and what would refuse it now",
        responses=problem_responses(_PREVIEW),
    )
    async def preview_vacation_mode(  # noqa: PLR0913, PLR0917 - the route's inputs
        shop_id: uuid.UUID,
        body: VacationMode,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWritePreviewView:
        preview = await _preview(
            auth, services, VACATION_MODE, organization_id, shop_id, body.model_dump()
        )
        response.headers["Cache-Control"] = "no-store"
        return preview

    @router.post(
        "/shops/{shop_id}/owner-writes/vacation-mode",
        summary="Set or clear the shop's vacation mode (the Owner's press)",
        responses=problem_responses(_PRESS),
    )
    async def run_vacation_mode(  # noqa: PLR0913, PLR0917 - the route's inputs
        shop_id: uuid.UUID,
        body: VacationMode,
        request: Request,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        idempotency_key: PressKey,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWriteRunView:
        """`etsy.shop.update` through the Action Gateway: re-reads the shop and sends nothing when
        it is already in that state (`sent` false), else sends once and re-reads."""
        run = await _press(
            request, auth, services, VACATION_MODE, organization_id, shop_id,
            body.model_dump(), idempotency_key,
        )  # fmt: skip
        response.headers["Cache-Control"] = "no-store"
        return run

    @router.get(
        "/relay-items/{item_id}/upload-preview",
        summary="What uploading a relay item's tracking would write",
        responses=problem_responses(
            {
                **_COMMON,
                404: "No such relay item for the caller (the Owner only)",
                409: "No pasted tracking waits for the upload (relay-item-moved)",
                422: ("Invalid ID", ValidationProblem),
                503: "The relay or the write actions are not set up (relay-unavailable, "
                "owner-writes-unavailable)",
            }
        ),
    )
    async def preview_relay_upload(
        item_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWritePreviewView:
        try:
            preview = await anyio.to_thread.run_sync(
                lambda: relay_upload_preview(
                    auth, services, organization_id=organization_id, item_id=item_id,
                    now=clock(),
                )
            )  # fmt: skip
        except _KNOWN as error:
            raise _problem(error) from None
        response.headers["Cache-Control"] = "no-store"
        return _preview_view(preview)

    @router.post(
        "/relay-items/{item_id}/upload",
        summary="Upload a relay item's pasted tracking to its Etsy order (the Owner's press)",
        responses=problem_responses(
            {
                **_PRESS,
                404: "No such relay item for the caller (the Owner only)",
                409: "No pasted tracking waits for the upload (relay-item-moved)",
                503: "The relay or the write actions are not set up (relay-unavailable, "
                "owner-writes-unavailable)",
            }
        ),
    )
    async def upload_relay_item(  # noqa: PLR0913, PLR0917 - the route's inputs
        item_id: uuid.UUID,
        request: Request,
        response: Response,
        auth: CurrentAuth,
        services: Services,
        idempotency_key: PressKey,
        organization_id: uuid.UUID | None = None,
    ) -> OwnerWriteRunView:
        """A verified result closes the item (`uploaded`, or `tracking_on_etsy` when the receipt
        had the code already); a refused or failed upload leaves it with the person who copies
        it by hand (AC-6.10.3)."""
        try:
            run = await upload_relay_tracking(
                auth, services, organization_id=organization_id, item_id=item_id,
                press_key=idempotency_key, correlation_id=_correlation(request), now=clock(),
            )  # fmt: skip
        except _KNOWN as error:
            raise _problem(error) from None
        response.headers["Cache-Control"] = "no-store"
        return _run_view(run)

    return router
