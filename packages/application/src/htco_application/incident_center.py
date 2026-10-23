"""The incident center and the notification inbox for people (slices 6.4, 6.5, FR-INC-06).

"View incidents" decides who reads an incident: Owner and Admin the whole organization, other
roles the incidents of their granted shops (an organization-wide incident only for Owner and
Admin). "Open or update incidents" decides who moves one along the machine; nobody resolves one by
hand (resolution needs evidence, slice 6.3). An incident the caller may not see does not exist
for them.

The inbox is each member's own: notifications addressed to them in that organization.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.incidents import IncidentRecord
from htco_application.notifications import Notification
from htco_application.shops import OrganizationRequiredError
from htco_domain.incidents import Evidence, IncidentState


class IncidentNotVisibleError(Exception):
    """No such incident for the caller."""


@dataclass(frozen=True)
class IncidentHistoryEntry:
    from_state: IncidentState | None
    to_state: IncidentState
    actor_kind: str
    actor_id: str
    at: datetime
    note: str | None
    evidence: Evidence | None


@dataclass(frozen=True)
class IncidentDetail:
    incident: IncidentRecord
    history: tuple[IncidentHistoryEntry, ...]


class IncidentReader(Protocol):
    """Synchronous; run in a worker thread."""

    def list_incidents(
        self,
        organization_id: uuid.UUID,
        *,
        open_only: bool,
        limit: int,
        shop_ids: frozenset[uuid.UUID] | None = None,
    ) -> Sequence[IncidentRecord]:
        """Newest first; `shop_ids` (a shop-scoped role's grants) is applied before `limit`
        and leaves out organization-wide incidents."""
        ...

    def incident_detail(
        self, organization_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentDetail | None: ...


def _organization(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return organization


def _visible(
    auth: AuthContext,
    organization: uuid.UUID,
    shop: uuid.UUID | None,
    capability: Capability,
    *,
    now: datetime,
) -> bool:
    principal = auth.principal_for(organization)  # NoMembershipError for a stranger
    overall = decide(principal, capability, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        return False
    if shop is None:
        return overall.scope is not Scope.GRANTED_SHOPS
    return (
        decide(
            principal, capability, shop=ShopRef(id=shop, organization_id=organization), now=now
        ).outcome
        is Outcome.ALLOW
    )


def list_incidents(  # noqa: PLR0913 - the caller, the reader and the filters
    auth: AuthContext,
    reader: IncidentReader,
    *,
    organization_id: uuid.UUID | None,
    open_only: bool,
    now: datetime,
    limit: int = 200,
) -> tuple[IncidentRecord, ...]:
    """Raises `PermissionError` for a role that may not view incidents."""
    organization = _organization(auth, organization_id)
    principal = auth.principal_for(organization)
    overall = decide(principal, Capability.VIEW_INCIDENTS, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not view incidents")
    # Shop scope in the query, before the limit (Codex review of #87).
    shops = principal.granted_shops if overall.scope is Scope.GRANTED_SHOPS else None
    rows = reader.list_incidents(organization, open_only=open_only, limit=limit, shop_ids=shops)
    return tuple(
        i
        for i in rows
        if _visible(auth, organization, i.shop_id, Capability.VIEW_INCIDENTS, now=now)
    )


def incident_detail(
    auth: AuthContext,
    reader: IncidentReader,
    incident_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> IncidentDetail:
    organization = _organization(auth, organization_id)
    detail = reader.incident_detail(organization, incident_id)
    if detail is None or not _visible(
        auth, organization, detail.incident.shop_id, Capability.VIEW_INCIDENTS, now=now
    ):
        raise IncidentNotVisibleError(str(incident_id))
    return detail


def may_update(auth: AuthContext, incident: IncidentRecord, *, now: datetime) -> bool:
    return _visible(
        auth, incident.organization_id, incident.shop_id, Capability.UPDATE_INCIDENTS, now=now
    )


# --- The inbox -----------------------------------------------------------------------------------


class NotificationInbox(Protocol):
    """Synchronous; run in a worker thread."""

    def inbox(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool, limit: int
    ) -> tuple[Sequence[Notification], int]:
        """The newest notifications and the number unread."""
        ...

    def mark_read(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        notification_ids: Sequence[uuid.UUID] | None,
        *,
        now: datetime,
    ) -> int:
        """Marks the user's notifications read (all of them when `notification_ids` is None)."""
        ...


def inbox_of(
    auth: AuthContext,
    inbox: NotificationInbox,
    *,
    organization_id: uuid.UUID | None,
    unread_only: bool,
    limit: int = 50,
) -> tuple[Sequence[Notification], int]:
    organization = _organization(auth, organization_id)
    auth.principal_for(organization)  # a member of the organization
    return inbox.inbox(organization, auth.user_id, unread_only=unread_only, limit=limit)


def mark_read(
    auth: AuthContext,
    inbox: NotificationInbox,
    notification_ids: Sequence[uuid.UUID] | None,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> int:
    organization = _organization(auth, organization_id)
    auth.principal_for(organization)
    return inbox.mark_read(organization, auth.user_id, notification_ids, now=now)
