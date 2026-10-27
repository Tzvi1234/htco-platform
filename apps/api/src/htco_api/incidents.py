"""The incident center and the notification inbox (slices 6.4, 6.5).

- `GET /v1/incidents`: the organization's incidents the caller may view (open ones by default),
  optionally one shop's or one order's (`shop_id`, `receipt_id`), in cursor pages.
- `GET /v1/incidents/{id}`: one incident with its history and evidence.
- `POST /v1/incidents/{id}/transitions`: a person's step along the machine ("Open or update
  incidents"); `resolved` is refused (409 `evidence-required`): incidents resolve on evidence.
- `GET /v1/notifications`, `POST /v1/notifications/read`: the caller's own inbox.
- `GET`/`PUT /v1/notifications/preferences`: the caller's own preferences per kind, per severity
  and the muted shops (slice 7.6). Security kinds are shown locked and cannot be set.

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
from htco_application.notification_preferences import (
    NotificationPreferenceStore,
    PreferenceRefusedError,
    PreferenceSet,
    default_preference,
    preferences_of,
    set_preferences,
)
from htco_application.notifications import Notification
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.incidents import IncidentState, ResolutionRefused
from htco_domain.notifications import NotificationKind, NotificationSeverity, Preference
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
    # Slice 7.6: each member's notification preferences; without it, those routes answer 503.
    preferences: NotificationPreferenceStore | None = None


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


class ChannelsView(BaseModel):
    in_app: bool
    email: bool


class KindPreferenceView(ChannelsView):
    kind: str
    # Security kinds always go out on both channels and cannot be changed.
    locked: bool


class SeverityPreferenceView(ChannelsView):
    severity: str
    # A critical notification always reaches the inbox: only its email can be turned off.
    in_app_locked: bool


class PreferencesView(BaseModel):
    kinds: list[KindPreferenceView]
    severities: list[SeverityPreferenceView]
    muted_shops: list[uuid.UUID]


class ChannelsChoice(BaseModel):
    in_app: bool
    email: bool


class PreferencesRequest(BaseModel):
    """The member's whole choice; anything left out returns to its default."""

    kinds: dict[NotificationKind, ChannelsChoice] = Field(default_factory=dict)
    severities: dict[NotificationSeverity, ChannelsChoice] = Field(default_factory=dict)
    muted_shops: list[uuid.UUID] = Field(default_factory=list, max_length=200)


PREFERENCE_REFUSED = ProblemType(
    "preference-refused", "Preference not allowed", 422,
    "Security notifications always go out, critical ones always reach the inbox, and only a "
    "shop you can see can be muted.",
)  # fmt: skip


def _preferences_view(stored: PreferenceSet) -> PreferencesView:
    kinds = []
    for kind in NotificationKind:
        chosen = stored.kinds.get(kind, default_preference(kind))
        if kind.is_security:
            chosen = Preference(in_app=True, email=True)
        kinds.append(
            KindPreferenceView(
                kind=kind.value, in_app=chosen.in_app, email=chosen.email, locked=kind.is_security
            )
        )
    severities = []
    for severity in NotificationSeverity:
        chosen = stored.severities.get(severity, Preference())
        critical = severity is NotificationSeverity.CRITICAL
        severities.append(
            SeverityPreferenceView(
                severity=severity.value, in_app=chosen.in_app or critical, email=chosen.email,
                in_app_locked=critical,
            )
        )  # fmt: skip
    return PreferencesView(
        kinds=kinds, severities=severities, muted_shops=sorted(stored.muted_shops)
    )


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

    def preferences_store(center: IncidentCenter) -> NotificationPreferenceStore:
        if center.preferences is None:
            raise ProblemError(INCIDENTS_UNAVAILABLE)
        return center.preferences

    @router.get(
        "/notifications/preferences",
        summary="The caller's notification preferences in the organization",
        responses=problem_responses({**_COMMON, 403: "Not a member of the organization"}),
    )
    async def get_notification_preferences(
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> PreferencesView:
        """Every kind and severity with its channels (defaults filled in) and the muted shops."""
        store = preferences_store(center)
        try:
            _, stored = await _run(
                lambda: preferences_of(auth, store, organization_id=organization_id)
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return _preferences_view(stored)

    @router.put(
        "/notifications/preferences",
        summary="Replace the caller's notification preferences",
        responses=problem_responses(
            {
                **_COMMON,
                403: "Not a member of the organization",
                422: (
                    "Invalid body, or a preference the rules refuse (preference-refused)",
                    ValidationProblem,
                ),
            }
        ),
    )
    async def put_notification_preferences(
        body: PreferencesRequest,
        response: Response,
        auth: CurrentAuth,
        center: Center,
        organization_id: uuid.UUID | None = None,
    ) -> PreferencesView:
        """Muting a shop stops its non-critical notifications; security ones always go out."""
        store = preferences_store(center)
        wanted = PreferenceSet(
            kinds={k: Preference(c.in_app, c.email) for k, c in body.kinds.items()},
            severities={s: Preference(c.in_app, c.email) for s, c in body.severities.items()},
            muted_shops=frozenset(body.muted_shops),
        )
        try:
            _, stored = await _run(
                lambda: set_preferences(
                    auth, store, wanted, organization_id=organization_id, now=clock()
                )
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        except PreferenceRefusedError as refused:
            raise ProblemError(PREFERENCE_REFUSED, extra={"reason": str(refused)}) from None
        response.headers["Cache-Control"] = "no-store"
        return _preferences_view(stored)

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
