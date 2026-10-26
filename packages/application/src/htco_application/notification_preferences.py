"""A member's own notification preferences (slices 6.4 and 7.6): read and replace them.

Per kind (in-app, email), per severity (in-app, email) and the shops the member muted. What a
member may set:

- **no security kind:** security notifications always go out (AC-6.4.4); asking to change one is
  refused rather than stored and ignored;
- **critical stays in the inbox:** a critical severity may turn off its email, never its in-app;
- **only their own shops:** Owners and Admins may mute any shop of the organization (the database
  checks that it belongs to it), and other roles only the shops granted to them.

Whatever is stored, `htco_domain.notifications.delivery` has the last word when a notification is
sent: a muted shop never silences a critical notification or a security one (AC-7.6.1).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from htco_application.access.authentication import AuthContext
from htco_application.shops import OrganizationRequiredError
from htco_domain.identity import Role
from htco_domain.notifications import (
    EMAIL_BY_DEFAULT,
    NotificationKind,
    NotificationSeverity,
    Preference,
)

MAX_MUTED_SHOPS = 200


class PreferenceRefusedError(ValueError):
    """The member asked for something the rules do not allow; the reason is safe to show."""


@dataclass(frozen=True)
class PreferenceSet:
    """What a member stored; anything missing takes its default."""

    kinds: Mapping[NotificationKind, Preference] = field(default_factory=dict)
    severities: Mapping[NotificationSeverity, Preference] = field(default_factory=dict)
    muted_shops: frozenset[uuid.UUID] = frozenset()


class NotificationPreferenceStore(Protocol):
    """Synchronous (the API's pool, the user in every query); run in a worker thread."""

    def read(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> PreferenceSet: ...

    def replace(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        preferences: PreferenceSet,
        *,
        now: datetime,
    ) -> None:
        """Replaces the member's three sets in one transaction."""
        ...


def _organization(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    auth.principal_for(organization)  # a member of the organization
    return organization


def default_preference(kind: NotificationKind) -> Preference:
    return Preference(in_app=True, email=kind in EMAIL_BY_DEFAULT)


def preferences_of(
    auth: AuthContext, store: NotificationPreferenceStore, *, organization_id: uuid.UUID | None
) -> tuple[uuid.UUID, PreferenceSet]:
    organization = _organization(auth, organization_id)
    return organization, store.read(organization, auth.user_id)


def check_preferences(auth: AuthContext, organization: uuid.UUID, wanted: PreferenceSet) -> None:
    """Raises `PreferenceRefusedError` with a reason safe to show."""
    locked = sorted(kind.value for kind in wanted.kinds if kind.is_security)
    if locked:
        raise PreferenceRefusedError(f"security notifications always go out: {', '.join(locked)}")
    critical = wanted.severities.get(NotificationSeverity.CRITICAL)
    if critical is not None and not critical.in_app:
        raise PreferenceRefusedError("critical notifications always reach the inbox")
    if len(wanted.muted_shops) > MAX_MUTED_SHOPS:
        raise PreferenceRefusedError(f"at most {MAX_MUTED_SHOPS} muted shops")
    principal = auth.principal_for(organization)
    if principal.role not in {Role.OWNER, Role.ADMIN} and not (
        wanted.muted_shops <= principal.granted_shops
    ):
        raise PreferenceRefusedError("a shop can be muted only by a member who can see it")


def set_preferences(
    auth: AuthContext,
    store: NotificationPreferenceStore,
    wanted: PreferenceSet,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> tuple[uuid.UUID, PreferenceSet]:
    organization = _organization(auth, organization_id)
    check_preferences(auth, organization, wanted)
    store.replace(organization, auth.user_id, wanted, now=now)
    return organization, store.read(organization, auth.user_id)
