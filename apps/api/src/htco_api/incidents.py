"""The incident center and the notification inbox (slices 6.4, 6.5).

- `GET /v1/incidents`: the organization's incidents the caller may view (open ones by default).
- `GET /v1/incidents/{id}`: one incident with its history and evidence.
- `POST /v1/incidents/{id}/transitions`: a person's step along the machine ("Open or update
  incidents"); `resolved` is refused (409 `evidence-required`): incidents resolve on evidence.
- `GET /v1/notifications`, `POST /v1/notifications/read`: the caller's own inbox.

Identifiers, states and codes only; never buyer data (an order is its receipt number).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from htco_api.auth import CurrentAuth
from htco_api.identity import ORGANIZATION_REQUIRED, problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, Problem, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.incident_center import (
    IncidentDetail,
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
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef
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


@dataclass(frozen=True)
class IncidentCenter:
    reader: IncidentReader
    service: IncidentService
    inbox: NotificationInbox


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
    async def get_incidents(
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
        state: Literal["open", "all"] = "open",
    ) -> IncidentListView:
        """Newest first; a shop-scoped role sees its granted shops' incidents only."""
        try:
            items = await _run(
                lambda: list_incidents(
                    auth, center.reader, organization_id=organization_id,
                    open_only=state == "open", now=clock(),
                )
            )  # fmt: skip
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except PermissionError, NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return IncidentListView(items=[IncidentSummaryView(**_summary(i)) for i in items])

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

    return router
