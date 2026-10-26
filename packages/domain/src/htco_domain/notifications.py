"""Notifications (slice 6.4, FR-NTF-01, FR-EML-01): kinds, who receives them, and what a
notification may say. No I/O.

A notification carries a kind and a few parameters; the screen and the email template render the
words in the reader's language. Parameters are identifiers only (FR-EML-01, AC-6.4.2): the
receipt number, never a buyer's name, address or tracking code.

Preferences (slice 7.6): a user chooses in-app and email per kind (6.4) and per severity, and can
mute a shop. Every notification has a severity (`severity_of`): security kinds are critical, an
incident carries its own, every other kind a fixed one. `delivery` decides the two channels:
security kinds always go out on both, whatever the preferences (AC-6.4.4, AC-7.6.1); a muted
shop's non-critical notifications go nowhere; a critical one always reaches the inbox.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class NotificationKind(StrEnum):
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_RESOLVED = "incident_resolved"
    INCIDENT_ESCALATED = "incident_escalated"  # slice 6.7
    ALERT_SHIP_BY = "alert_ship_by"  # slice 6.9
    ALERT_LOW_REVIEW = "alert_low_review"  # slice 6.9
    ALERT_OUT_OF_STOCK = "alert_out_of_stock"  # slice 6.9
    RELAY_TRACKING_NEEDED = "relay_tracking_needed"  # slice 6.10
    SIGN_IN_NEW_DEVICE = "sign_in_new_device"
    MFA_CHANGED = "mfa_changed"
    SHOP_CONNECTED = "shop_connected"
    SHOP_DISCONNECTED = "shop_disconnected"
    INTEGRATION_REAUTH = "integration_reauth"
    REFRESH_TOKEN_EXPIRING = "refresh_token_expiring"  # noqa: S105 - a kind, not a secret

    @property
    def is_security(self) -> bool:
        return self in SECURITY_KINDS


SECURITY_KINDS = frozenset(
    {
        NotificationKind.SIGN_IN_NEW_DEVICE,
        NotificationKind.MFA_CHANGED,
        NotificationKind.SHOP_CONNECTED,
        NotificationKind.SHOP_DISCONNECTED,
        NotificationKind.INTEGRATION_REAUTH,
        NotificationKind.REFRESH_TOKEN_EXPIRING,
    }
)


class Audience(StrEnum):
    """Who receives a kind: the user it is about, or the organization's owners (and admins)."""

    THE_USER = "the_user"
    OWNERS = "owners"
    OWNERS_AND_ADMINS = "owners_and_admins"


AUDIENCE: Mapping[NotificationKind, Audience] = {
    NotificationKind.INCIDENT_OPENED: Audience.OWNERS_AND_ADMINS,
    NotificationKind.INCIDENT_RESOLVED: Audience.OWNERS_AND_ADMINS,
    NotificationKind.INCIDENT_ESCALATED: Audience.OWNERS_AND_ADMINS,
    NotificationKind.ALERT_SHIP_BY: Audience.OWNERS_AND_ADMINS,
    NotificationKind.ALERT_LOW_REVIEW: Audience.OWNERS_AND_ADMINS,
    NotificationKind.ALERT_OUT_OF_STOCK: Audience.OWNERS_AND_ADMINS,
    NotificationKind.RELAY_TRACKING_NEEDED: Audience.OWNERS,
    NotificationKind.SIGN_IN_NEW_DEVICE: Audience.THE_USER,
    NotificationKind.MFA_CHANGED: Audience.OWNERS,
    NotificationKind.SHOP_CONNECTED: Audience.OWNERS,
    NotificationKind.SHOP_DISCONNECTED: Audience.OWNERS,
    NotificationKind.INTEGRATION_REAUTH: Audience.OWNERS,
    NotificationKind.REFRESH_TOKEN_EXPIRING: Audience.OWNERS,
}

# Kinds that also send an email by default (a preference can turn an email off, never a
# security email: those always go out, AC-6.4.4).
EMAIL_BY_DEFAULT = SECURITY_KINDS | {
    NotificationKind.INCIDENT_OPENED,
    NotificationKind.INCIDENT_ESCALATED,
    NotificationKind.ALERT_SHIP_BY,
    NotificationKind.ALERT_LOW_REVIEW,
    NotificationKind.RELAY_TRACKING_NEEDED,
}


class Locale(StrEnum):
    HE = "he"
    EN = "en"


# What a notification may say: identifiers, codes and small figures only.
_PARAMS: Mapping[str, re.Pattern[str]] = {
    "receipt_id": re.compile(r"^[1-9]\d{0,18}$"),
    "listing_id": re.compile(r"^[1-9]\d{0,18}$"),
    "incident_id": re.compile(r"^[0-9a-f-]{36}$"),
    "relay_item_id": re.compile(r"^[0-9a-f-]{36}$"),
    "shop_id": re.compile(r"^[0-9a-f-]{36}$"),
    "cause": re.compile(r"^[a-z_]{1,40}$"),
    "severity": re.compile(r"^(low|medium|high|critical)$"),
    "rating": re.compile(r"^[1-5]$"),
    "ship_by": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "change": re.compile(r"^(enrolled|removed)$"),
    "device": re.compile(r"^[A-Za-z0-9 .()/_-]{1,60}$"),
}


class UnsafeParameter(ValueError):  # noqa: N818 - a domain error named for what happened
    """A parameter outside the allowlist, or a value that does not look like an identifier."""


def check_params(params: Mapping[str, object]) -> dict[str, str]:
    """The parameters as strings, refusing anything outside the allowlist (AC-6.4.2)."""
    checked: dict[str, str] = {}
    for name, value in params.items():
        pattern = _PARAMS.get(name)
        text = str(value)
        if pattern is None or not pattern.match(text):
            raise UnsafeParameter(f"notification parameter {name!r} is not allowed")
        checked[name] = text
    return checked


@dataclass(frozen=True)
class Preference:
    """One user's choice for one kind; no row means the default."""

    in_app: bool = True
    email: bool = True


def wants_email(kind: NotificationKind, preference: Preference | None) -> bool:
    if kind.is_security:
        return True
    if preference is None:
        return kind in EMAIL_BY_DEFAULT
    return preference.email


def wants_in_app(kind: NotificationKind, preference: Preference | None) -> bool:
    return kind.is_security or preference is None or preference.in_app


class NotificationSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# The severity of each kind; an incident's own severity parameter takes precedence.
KIND_SEVERITY: Mapping[NotificationKind, NotificationSeverity] = {
    NotificationKind.INCIDENT_OPENED: NotificationSeverity.HIGH,
    NotificationKind.INCIDENT_RESOLVED: NotificationSeverity.LOW,
    NotificationKind.INCIDENT_ESCALATED: NotificationSeverity.HIGH,
    NotificationKind.ALERT_SHIP_BY: NotificationSeverity.HIGH,
    NotificationKind.ALERT_LOW_REVIEW: NotificationSeverity.MEDIUM,
    NotificationKind.ALERT_OUT_OF_STOCK: NotificationSeverity.MEDIUM,
    NotificationKind.RELAY_TRACKING_NEEDED: NotificationSeverity.HIGH,
    **dict.fromkeys(SECURITY_KINDS, NotificationSeverity.CRITICAL),
}

_CARRIES_SEVERITY = frozenset(
    {
        NotificationKind.INCIDENT_OPENED,
        NotificationKind.INCIDENT_RESOLVED,
        NotificationKind.INCIDENT_ESCALATED,
    }
)


def severity_of(kind: NotificationKind, params: Mapping[str, str]) -> NotificationSeverity:
    if kind.is_security:
        return NotificationSeverity.CRITICAL
    own = params.get("severity")
    if kind in _CARRIES_SEVERITY and own is not None:
        return NotificationSeverity(own)
    return KIND_SEVERITY[kind]


@dataclass(frozen=True)
class Preferences:
    """What one recipient chose for one notification: its kind, its severity, its shop."""

    kind: Preference | None = None
    severity: Preference | None = None
    shop_muted: bool = False


def delivery(
    kind: NotificationKind, severity: NotificationSeverity, preferences: Preferences
) -> tuple[bool, bool]:
    """(in-app, email) for one recipient (slice 7.6)."""
    if kind.is_security:
        return True, True
    critical = severity is NotificationSeverity.CRITICAL
    if preferences.shop_muted and not critical:
        return False, False
    by_severity = preferences.severity or Preference()
    in_app = critical or (wants_in_app(kind, preferences.kind) and by_severity.in_app)
    email = wants_email(kind, preferences.kind) and by_severity.email
    return in_app, email
