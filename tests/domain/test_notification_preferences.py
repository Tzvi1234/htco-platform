"""Notification preferences per severity and per shop (slice 7.6).

Each notification has a severity: security kinds are critical, an incident carries its own, and
every other kind has a fixed one. A user chooses in-app and email per kind (6.4) and per severity,
and can mute a shop. Muting a shop stops its non-critical notifications; critical security
notifications can never be muted, by any preference (AC-7.6.1)."""

import pytest

from htco_domain.notifications import (
    KIND_SEVERITY,
    NotificationKind,
    NotificationSeverity,
    Preference,
    Preferences,
    delivery,
    severity_of,
)

K = NotificationKind
S = NotificationSeverity
OFF = Preference(in_app=False, email=False)


def test_severity__security_kinds_are_critical() -> None:
    for kind in K:
        if kind.is_security:
            assert severity_of(kind, {}) is S.CRITICAL


def test_severity__an_incident_carries_its_own() -> None:
    assert severity_of(K.INCIDENT_OPENED, {"severity": "high"}) is S.HIGH
    assert severity_of(K.INCIDENT_ESCALATED, {"severity": "critical"}) is S.CRITICAL
    assert severity_of(K.INCIDENT_OPENED, {}) is KIND_SEVERITY[K.INCIDENT_OPENED]


def test_severity__every_kind_has_one() -> None:
    assert set(KIND_SEVERITY) == set(K)


@pytest.mark.ac("AC-7.6.1")
def test_shop_muted__stops_a_non_critical_notification_on_both_channels() -> None:
    muted = Preferences(shop_muted=True)
    for severity in (S.LOW, S.MEDIUM, S.HIGH):
        assert delivery(K.ALERT_LOW_REVIEW, severity, muted) == (False, False)


@pytest.mark.ac("AC-7.6.1")
def test_shop_muted__a_critical_notification_still_arrives() -> None:
    assert delivery(K.INCIDENT_OPENED, S.CRITICAL, Preferences(shop_muted=True)) == (True, True)


@pytest.mark.ac("AC-7.6.1")
@pytest.mark.parametrize("kind", sorted(k for k in K if k.is_security))
def test_security__can_never_be_muted_by_any_preference(kind: NotificationKind) -> None:
    everything_off = Preferences(kind=OFF, severity=OFF, shop_muted=True)
    assert delivery(kind, severity_of(kind, {}), everything_off) == (True, True)


def test_severity_preference__turns_a_channel_off_for_that_severity() -> None:
    no_email = Preferences(severity=Preference(in_app=True, email=False))
    assert delivery(K.ALERT_SHIP_BY, S.HIGH, no_email) == (True, False)


def test_severity_preference__critical_stays_in_the_inbox() -> None:
    """A critical notification may skip email by choice, never the inbox."""
    off = Preferences(severity=OFF)
    assert delivery(K.INCIDENT_OPENED, S.CRITICAL, off) == (True, False)


def test_kind_and_severity__both_must_want_a_channel() -> None:
    kind_off = Preferences(kind=Preference(in_app=False, email=True))
    assert delivery(K.ALERT_SHIP_BY, S.HIGH, kind_off) == (False, True)
    both = Preferences(
        kind=Preference(in_app=True, email=True), severity=Preference(in_app=True, email=False)
    )
    assert delivery(K.ALERT_SHIP_BY, S.HIGH, both) == (True, False)


def test_defaults__the_kinds_defaults_from_6_4() -> None:
    nothing = Preferences()
    assert delivery(K.INCIDENT_OPENED, S.HIGH, nothing) == (True, True)
    assert delivery(K.INCIDENT_RESOLVED, S.LOW, nothing) == (True, False)
    assert delivery(K.ALERT_OUT_OF_STOCK, S.MEDIUM, nothing) == (True, False)
