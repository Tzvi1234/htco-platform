"""The incident machine, deduplication keys, resolution on evidence and escalation (6.3, 6.7)."""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from htco_domain.incidents import (
    INCIDENT_TRANSITIONS,
    Cause,
    Evidence,
    EvidenceKind,
    IncidentState,
    ResolutionRefused,
    ResourceKind,
    Severity,
    check_incident_transition,
    dedup_key,
    resolution_path,
    should_escalate,
)
from htco_domain.state_machine import IllegalTransition

_I = IncidentState
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ALLOWED = [(a, b) for a, targets in INCIDENT_TRANSITIONS.items() for b in sorted(targets)]
ILLEGAL = [
    (a, b) for a, b in product(IncidentState, IncidentState) if b not in INCIDENT_TRANSITIONS[a]
]
EVIDENCE = Evidence(EvidenceKind.TRACKING_PRESENT, NOW, "etsy_receipt", "tracking-event-1")


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed_transitions_pass(current: IncidentState, target: IncidentState) -> None:
    check_incident_transition(current, target)


@pytest.mark.parametrize(("current", "target"), ILLEGAL)
def test_ac_6_3_3_every_illegal_transition_is_rejected(
    current: IncidentState, target: IncidentState
) -> None:
    with pytest.raises(IllegalTransition):
        check_incident_transition(current, target)


def test_the_machine_follows_fr_inc_02() -> None:
    assert set(INCIDENT_TRANSITIONS) == set(IncidentState)
    assert INCIDENT_TRANSITIONS[_I.RESOLVED] == frozenset()
    # RESOLVED only from VERIFYING.
    assert [a for a, targets in INCIDENT_TRANSITIONS.items() if _I.RESOLVED in targets] == [
        _I.VERIFYING
    ]
    # ESCALATED from every open state but itself.
    for state in IncidentState:
        if state not in {_I.RESOLVED, _I.ESCALATED}:
            assert _I.ESCALATED in INCIDENT_TRANSITIONS[state]
    assert not _I.RESOLVED.is_open
    assert _I.ESCALATED.is_open


@pytest.mark.parametrize("state", [s for s in IncidentState if s is not _I.RESOLVED])
def test_ac_6_3_2_evidence_resolves_from_every_open_state_along_legal_edges(
    state: IncidentState,
) -> None:
    path = resolution_path(state, EVIDENCE)
    assert path[-1] is _I.RESOLVED
    current = state
    for following in path:
        check_incident_transition(current, following)
        current = following


@pytest.mark.parametrize("state", list(IncidentState))
def test_ac_6_3_2_without_evidence_an_incident_never_resolves(state: IncidentState) -> None:
    with pytest.raises(ResolutionRefused):
        resolution_path(state, None)


def test_a_resolved_incident_does_not_resolve_again() -> None:
    with pytest.raises(IllegalTransition):
        resolution_path(_I.RESOLVED, EVIDENCE)


def test_evidence_names_its_source_and_reference() -> None:
    with pytest.raises(ValueError, match="source and reference"):
        Evidence(EvidenceKind.TRACKING_PRESENT, NOW, "", "x")
    with pytest.raises(ValueError, match="source and reference"):
        Evidence(EvidenceKind.TRACKING_PRESENT, NOW, "etsy_receipt", "")


def test_ac_6_3_1_one_dedup_key_per_cause_and_resource() -> None:
    a = dedup_key(Cause.TRACKING_MISSING, ResourceKind.ORDER, "shop-1/123")
    assert a == "tracking_missing:order:shop-1/123"
    assert a == dedup_key(Cause.TRACKING_MISSING, ResourceKind.ORDER, "shop-1/123")
    assert a != dedup_key(Cause.SYNC_DRIFT, ResourceKind.ORDER, "shop-1/123")
    for bad in ["", "a:b", "x" * 121]:
        with pytest.raises(ValueError, match="resource ID"):
            dedup_key(Cause.TRACKING_MISSING, ResourceKind.ORDER, bad)


@pytest.mark.parametrize(
    ("severity", "age", "expected"),
    [
        (Severity.HIGH, timedelta(hours=24), False),
        (Severity.HIGH, timedelta(hours=24, seconds=1), True),
        (Severity.CRITICAL, timedelta(hours=5), True),
        (Severity.MEDIUM, timedelta(days=2), False),
        (Severity.LOW, timedelta(days=8), True),
    ],
)
def test_escalation_by_severity_and_age(severity: Severity, age: timedelta, expected: bool) -> None:
    assert should_escalate(_I.DETECTED, severity, detected_at=NOW - age, now=NOW) is expected


def test_incidents_in_hand_or_resolved_do_not_escalate() -> None:
    old = NOW - timedelta(days=30)
    for state in [_I.VERIFYING, _I.APPROVAL_REQUIRED, _I.RESOLVED, _I.ESCALATED]:
        assert not should_escalate(state, Severity.CRITICAL, detected_at=old, now=NOW)


def test_incident_states_map_to_the_office_status_vocabulary() -> None:
    """Architecture audit P2-2: the incident machine speaks the 5.18 status words."""
    from htco_domain.status import StatusKind, status_kind  # noqa: PLC0415

    assert status_kind(IncidentState.RESOLVED) is StatusKind.SUCCEEDED
    assert status_kind(IncidentState.APPROVAL_REQUIRED) is StatusKind.WAITING
    assert {status_kind(s) for s in IncidentState if s.is_open} <= {
        StatusKind.ACTIVE,
        StatusKind.WAITING,
    }
