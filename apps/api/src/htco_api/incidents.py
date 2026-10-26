"""The incident center and the notification inbox (slices 6.4, 6.5).

- `GET /v1/incidents`: the organization's incidents the caller may view (open ones by default),
  optionally one shop's or one order's (`shop_id`, `receipt_id`), in cursor pages.
- `GET /v1/incidents/{id}`: one incident with its history and evidence.
- `POST /v1/incidents/{id}/transitions`: a person's step along the machine ("Open or update
  incidents"); `resolved` is refused (409 `evidence-required`): incidents resolve on evidence.
- `GET /v1/notifications`, `POST /v1/notifications/read`: the caller's own inbox.
- `GET /v1/relay-items`, `POST /v1/relay-items/{id}/tracking`, `GET /v1/relay-items/{id}/code`:
  the AutoVA relay (slice 6.10): the open items, the owner's paste, and the code for the person
  who copies it into Etsy (buyer-data roles only; never in a list, a log or an audit).

Identifiers, states and codes only; never buyer data (an order is its receipt number).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from htco_api.auth import CurrentAuth
from htco_api.identity import (
    ORGANIZATION_REQUIRED,
    decode_cursor,
    encode_cursor,
    problem_responses,
)
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, Problem, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.incident_center import (
    IncidentDetail,
    IncidentFilter,
    IncidentNotVisibleError,
    IncidentReader,
    NotificationInbox,
    inbox_of,
    incident_detail,
    list_incidents,
    mark_read,
    may_update,
)
from htco_application.incidents import IncidentRecord, IncidentService, StaleIncident
from htco_application.notifications import Notification
from htco_application.relay import (
    DuplicateTrackingError,
    RelayItem,
    RelayItemMovedError,
    RelayItemNotVisibleError,
    RelayStore,
    list_relay_items,
    relay_code,
    submit_tracking,
)
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import TrackingFormatError
from htco_domain.incidents import IncidentState, ResolutionRefused
from htco_domain.state_machine import IllegalTransition

INCIDENTS_UNAVAILABLE = ProblemType(
    "incidents-unavailable", "Incidents not available", 503,
    "Incidents cannot be read on this server.",
)  # fmt: skip
EVIDENCE_REQUIRED = ProblemType(
    "evidence-required", "Evidence required", 409,
    "An incident is resolved only when the evidence arrives, for example tracking in Etsy.",
)  # fmt: skip
ILLEGAL_STEP = ProblemType(
    "illegal-incident-step", "Step not allowed", 409,
    "The incident cannot move to that state from where it is now.",
)  # fmt: skip
INCIDENT_MOVED = ProblemType(
    "incident-moved", "Incident changed", 409,
    "The incident changed meanwhile. Reload it and try again.",
)  # fmt: skip


RELAY_UNAVAILABLE = ProblemType(
    "relay-unavailable", "Relay not available", 503,
    "The AutoVA relay is not set up on this server.",
)  # fmt: skip
RELAY_ITEM_MOVED = ProblemType(
    "relay-item-moved", "Relay item changed", 409,
    "This order's tracking was pasted already, or the item closed. Reload it.",
)  # fmt: skip
TRACKING_ALREADY_USED = ProblemType(
    "tracking-already-used", "Tracking number in use", 409,
    "This tracking number is already pasted for another order of the shop.",
)  # fmt: skip


@dataclass(frozen=True)
class IncidentCenter:
    reader: IncidentReader
    service: IncidentService
    inbox: NotificationInbox
    # The AutoVA relay (slice 6.10); None: its routes answer relay-unavailable.
    relay: RelayStore | None = None


class RelayItemView(BaseModel):
    """No code: the last four characters only (the code is buyer data)."""

    id: uuid.UUID
    shop_id: uuid.UUID
    receipt_id: int
    state: Literal["awaiting_tracking", "handed_off", "closed"]
    carrier: str | None
    last4: str | None
    closed_reason: Literal["tracking_on_etsy", "uploaded", "cancelled"] | None
    paid_at: datetime
    opened_at: datetime
    updated_at: datetime


class RelayItemListView(BaseModel):
    items: list[RelayItemView]


class TrackingPaste(BaseModel):
    carrier: str = Field(min_length=1, max_length=20)
    # Spaces and dashes as AutoVA shows them are fine; the number is normalised and validated.
    code: str = Field(min_length=1, max_length=60)


class RelayCodeView(BaseModel):
    code: str


def _relay_view(item: RelayItem) -> RelayItemView:
    return RelayItemView(
        id=item.id, shop_id=item.shop_id, receipt_id=item.receipt_id, state=item.state.value,
        carrier=item.carrier, last4=item.last4,
        closed_reason=None if item.closed_reason is None else item.closed_reason.value,
        paid_at=item.paid_at, opened_at=item.opened_at, updated_at=item.updated_at,
    )  # fmt: skip


class IncidentSummaryView(BaseModel):
    id: uuid.UUID
    shop_id: uuid.UUID | None
    receipt_id: int | None
    cause: str
    severity: str
    state: str
    is_open: bool
    source: str
    detected_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class IncidentListView(BaseModel):
    items: list[IncidentSummaryView]
    # Pass it back as `cursor` for the next page; null on the last page.
    next_cursor: str | None = None


class EvidenceView(BaseModel):
    kind: str
    source: str
    reference: str
    observed_at: datetime


class HistoryEntryView(BaseModel):
    from_state: str | None
    to_state: str
    actor_kind: str
    actor_id: str
    at: datetime
    note: str | None
    evidence: EvidenceView | None


class IncidentDetailView(IncidentSummaryView):
    details: dict[str, Any]
    history: list[HistoryEntryView]
    can_update: bool
    next_states: list[str]


class TransitionRequest(BaseModel):
    to_state: Literal[
        "triaged", "investigating", "remediation_proposed", "auto_remediating",
        "approval_required", "developer_required", "verifying", "escalated", "resolved",
    ]  # fmt: skip
    note: str | None = Field(default=None, pattern=r"^[a-z0-9_.:-]{1,64}$")


class NotificationView(BaseModel):
    id: uuid.UUID
    kind: str
    params: dict[str, str]
    shop_id: uuid.UUID | None
    incident_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None


class InboxView(BaseModel):
    items: list[NotificationView]
    unread_count: int


class MarkReadRequest(BaseModel):
    # None: every unread notification of the caller in the organization.
    ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


def _center(request: Request) -> IncidentCenter:
    found = getattr(request.app.state, "incident_center", None)
    if found is None:
        raise ProblemError(INCIDENTS_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


def _summary(i: IncidentRecord) -> dict[str, Any]:
    return {
        "id": i.id, "shop_id": i.shop_id, "receipt_id": i.receipt_id, "cause": i.cause.value,
        "severity": i.severity.value, "state": i.state.value, "is_open": i.state.is_open,
        "source": i.source, "detected_at": i.detected_at, "updated_at": i.updated_at,
        "resolved_at": i.resolved_at,
    }  # fmt: skip


_PERSON_STEPS = frozenset(IncidentState) - {IncidentState.RESOLVED, IncidentState.DETECTED}


def _detail(detail: IncidentDetail, *, can_update: bool) -> IncidentDetailView:
    from htco_domain.incidents import INCIDENT_TRANSITIONS  # noqa: PLC0415

    incident = detail.incident
    return IncidentDetailView(
        **_summary(incident),
        details=dict(incident.details),
        history=[
            HistoryEntryView(
                from_state=None if e.from_state is None else e.from_state.value,
                to_state=e.to_state.value, actor_kind=e.actor_kind, actor_id=e.actor_id,
                at=e.at, note=e.note,
                evidence=None if e.evidence is None else EvidenceView(
                    kind=e.evidence.kind.value, source=e.evidence.source,
                    reference=e.evidence.reference, observed_at=e.evidence.observed_at,
                ),
            )
            for e in detail.history
        ],
        can_update=can_update,
        next_states=sorted(
            s.value for s in INCIDENT_TRANSITIONS[incident.state] if s in _PERSON_STEPS
        ) if can_update else [],
    )  # fmt: skip


def _notification(n: Notification) -> NotificationView:
    return NotificationView(
        id=n.id, kind=n.kind.value, params=dict(n.params), shop_id=n.shop_id,
        incident_id=n.incident_id, created_at=n.created_at, read_at=n.read_at,
    )  # fmt: skip


_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    400: "The caller belongs to several organizations and named none",
    422: ("Invalid ID or organization", ValidationProblem),
    503: "Incidents cannot be read on this server (incidents-unavailable)",
}


def incidents_router(*, clock: Callable[[], datetime]) -> APIRouter:  # noqa: PLR0915 - one nested function per route
    router = APIRouter(prefix="/v1", tags=["incidents"])
    Center = Annotated[IncidentCenter, Depends(_center)]  # noqa: N806 - a type alias

    def _run[T](work: Callable[[], T]) -> Any:
        return anyio.to_thread.run_sync(work)

    @router.get(
        "/incidents",
        summary="The organization's incidents the caller may view",
        responses=problem_responses({**_COMMON, 403: "The role may not view incidents"}),
    )
    async def get_incidents(  # noqa: PLR0913, PLR0917 - the filters and the page
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
        state: Literal["open", "all"] = "open",
        shop_id: uuid.UUID | None = None,
        receipt_id: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
    ) -> IncidentListView:
        """Newest first; a shop-scoped role sees its granted shops' incidents only.
        `shop_id` and `receipt_id` narrow the list to one shop or one order; pass
        `next_cursor` back as `cursor` for the next page."""
        before = decode_cursor(cursor) if cursor is not None else None
        try:
            page = await _run(
                lambda: list_incidents(
                    auth, center.reader, organization_id=organization_id,
                    open_only=state == "open", now=clock(), limit=limit,
                    where=IncidentFilter(shop_id=shop_id, receipt_id=receipt_id), before=before,
                )
            )  # fmt: skip
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError, NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return IncidentListView(
            items=[IncidentSummaryView(**_summary(i)) for i in page.items],
            next_cursor=None if page.next_before is None else encode_cursor(page.next_before),
        )

    @router.get(
        "/incidents/{incident_id}",
        summary="One incident with its history and evidence",
        responses=problem_responses({**_COMMON, 404: "No such incident for the caller"}),
    )
    async def get_incident(
        incident_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> IncidentDetailView:
        now = clock()
        try:
            detail = await _run(
                lambda: incident_detail(
                    auth, center.reader, incident_id, organization_id=organization_id, now=now
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except IncidentNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        response.headers["Cache-Control"] = "no-store"
        return _detail(detail, can_update=may_update(auth, detail.incident, now=now))

    @router.post(
        "/incidents/{incident_id}/transitions",
        summary="Move an incident one step along its machine",
        responses=problem_responses(
            {
                **_COMMON,
                403: "The role may not update incidents",
                404: "No such incident for the caller",
                409: "Not allowed from its state (illegal-incident-step), resolution without "
                "evidence (evidence-required), or it changed meanwhile (incident-moved)",
            }
        ),
    )
    async def move_incident(
        incident_id: uuid.UUID,
        body: TransitionRequest,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> IncidentDetailView:
        now = clock()
        try:
            detail = await _run(
                lambda: incident_detail(
                    auth, center.reader, incident_id, organization_id=organization_id, now=now
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except IncidentNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        if not may_update(auth, detail.incident, now=now):
            raise ProblemError(FORBIDDEN)
        try:
            await center.service.move(
                detail.incident.organization_id, incident_id, IncidentState(body.to_state),
                actor=ActorRef(ActorKind.USER, str(auth.user_id)), note=body.note or "by_person",
            )  # fmt: skip
        except ResolutionRefused:
            raise ProblemError(EVIDENCE_REQUIRED) from None
        except IllegalTransition:
            raise ProblemError(ILLEGAL_STEP) from None
        except StaleIncident:
            raise ProblemError(INCIDENT_MOVED) from None
        after = await _run(
            lambda: incident_detail(
                auth, center.reader, incident_id, organization_id=organization_id, now=now
            )
        )
        return _detail(after, can_update=may_update(auth, after.incident, now=now))

    @router.get(
        "/notifications",
        summary="The caller's notifications in the organization",
        responses=problem_responses({**_COMMON, 403: "Not a member of the organization"}),
    )
    async def get_notifications(
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
        unread_only: bool = False,
    ) -> InboxView:
        try:
            items, unread = await _run(
                lambda: inbox_of(
                    auth, center.inbox, organization_id=organization_id, unread_only=unread_only
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return InboxView(items=[_notification(n) for n in items], unread_count=unread)

    @router.post(
        "/notifications/read",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Mark the caller's notifications read",
        responses=problem_responses({**_COMMON, 403: "Not a member of the organization"}),
    )
    async def read_notifications(
        body: MarkReadRequest,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> Response:
        try:
            await _run(
                lambda: mark_read(
                    auth, center.inbox, body.ids, organization_id=organization_id, now=clock()
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def _relay(center: IncidentCenter) -> RelayStore:
        if center.relay is None:
            raise ProblemError(RELAY_UNAVAILABLE)
        return center.relay

    relay_problems: dict[int, str | tuple[str, type[Problem]]] = {
        **_COMMON,
        503: "The relay is not set up on this server (relay-unavailable)",
    }

    @router.get(
        "/relay-items",
        summary="The open AutoVA relay items of the caller's shops",
        responses=problem_responses({**relay_problems, 403: "The role may not act on fulfillment"}),
    )
    async def get_relay_items(
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> RelayItemListView:
        """Oldest first: each order waiting for its tracking from AutoVA (slice 6.10)."""
        store = _relay(center)
        try:
            items = await _run(
                lambda: list_relay_items(auth, store, organization_id=organization_id, now=clock())
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError, NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return RelayItemListView(items=[_relay_view(i) for i in items])

    @router.post(
        "/relay-items/{item_id}/tracking",
        summary="Paste the tracking number from AutoVA for one relay item",
        responses=problem_responses(
            {
                **relay_problems,
                403: "The role may not paste tracking",
                404: "No such relay item for the caller",
                409: "Pasted already or closed (relay-item-moved), or the number is used for "
                "another order (tracking-already-used)",
                422: (
                    "An unknown carrier or a number in the wrong format (tracking-invalid)",
                    Problem,
                ),
            }
        ),
    )
    async def paste_tracking(  # noqa: PLR0913, PLR0917 - the route's inputs
        item_id: uuid.UUID,
        body: TrackingPaste,
        request: Request,
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> RelayItemView:
        """Validated before anything else (AC-6.10.4). A number the Etsy receipt already carries
        closes the item on that evidence (AC-6.10.2); otherwise the item is handed to a person to
        copy into Etsy (AC-6.10.3). No Etsy call."""
        store = _relay(center)
        correlation = getattr(request.state, "request_id", None)
        try:
            item = await _run(
                lambda: submit_tracking(
                    auth, store, organization_id=organization_id, item_id=item_id,
                    carrier=body.carrier, code=body.code, now=clock(), correlation_id=correlation,
                )
            )  # fmt: skip
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except RelayItemNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        except PermissionError:
            raise ProblemError(FORBIDDEN) from None
        except TrackingFormatError as error:
            # The message is safe to show: it names the carrier, never the number.
            invalid = ProblemType("tracking-invalid", "Invalid tracking", 422, str(error))
            raise ProblemError(invalid) from None
        except RelayItemMovedError:
            raise ProblemError(RELAY_ITEM_MOVED) from None
        except DuplicateTrackingError:
            raise ProblemError(TRACKING_ALREADY_USED) from None
        response.headers["Cache-Control"] = "no-store"
        return _relay_view(item)

    @router.get(
        "/relay-items/{item_id}/code",
        summary="The pasted tracking number, for the person who copies it into Etsy",
        responses=problem_responses(
            {**relay_problems, 404: "No code for the caller (none, closed or not allowed)"}
        ),
    )
    async def get_relay_code(
        item_id: uuid.UUID,
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> RelayCodeView:
        """Buyer-data roles of the shop only; never cached."""
        store = _relay(center)
        try:
            code = await _run(
                lambda: relay_code(
                    auth, store, organization_id=organization_id, item_id=item_id, now=clock()
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except RelayItemNotVisibleError, NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        response.headers["Cache-Control"] = "no-store"
        return RelayCodeView(code=code)

    return router
