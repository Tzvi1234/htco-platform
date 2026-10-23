"""Incidents (slice 6.3, FR-INC-02, FR-INC-03, FR-INC-05). No I/O.

The machine: `DETECTED → TRIAGED → INVESTIGATING → REMEDIATION_PROPOSED → (AUTO_REMEDIATING |
APPROVAL_REQUIRED | DEVELOPER_REQUIRED) → VERIFYING → RESOLVED`, and `ESCALATED` from any open
state. Evidence can arrive at any open state: the incident then moves to `VERIFYING` (the path
it takes is recorded edge by edge, like the order machine) and, when the evidence verifies the
fix, to `RESOLVED`. `RESOLVED` is reached only from `VERIFYING`, and only with evidence: an
unhandled incident is never marked resolved.

Deduplication (FR-INC-03): one open incident per root cause and resource. The key is
`{cause}:{resource_kind}:{resource_id}`; the store keeps it unique among open incidents.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from htco_domain.state_machine import IllegalTransition, check_transition


class IncidentState(StrEnum):
    DETECTED = "detected"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    REMEDIATION_PROPOSED = "remediation_proposed"
    AUTO_REMEDIATING = "auto_remediating"
    APPROVAL_REQUIRED = "approval_required"
    DEVELOPER_REQUIRED = "developer_required"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"

    @property
    def is_open(self) -> bool:
        return self is not IncidentState.RESOLVED


_I = IncidentState
_REMEDIES = frozenset({_I.AUTO_REMEDIATING, _I.APPROVAL_REQUIRED, _I.DEVELOPER_REQUIRED})
INCIDENT_TRANSITIONS: Mapping[IncidentState, frozenset[IncidentState]] = {
    _I.DETECTED: frozenset({_I.TRIAGED, _I.VERIFYING, _I.ESCALATED}),
    _I.TRIAGED: frozenset({_I.INVESTIGATING, _I.VERIFYING, _I.ESCALATED}),
    _I.INVESTIGATING: frozenset({_I.REMEDIATION_PROPOSED, _I.VERIFYING, _I.ESCALATED}),
    _I.REMEDIATION_PROPOSED: _REMEDIES | {_I.ESCALATED},
    _I.AUTO_REMEDIATING: frozenset({_I.VERIFYING, _I.ESCALATED}),
    _I.APPROVAL_REQUIRED: frozenset({_I.VERIFYING, _I.ESCALATED}),
    _I.DEVELOPER_REQUIRED: frozenset({_I.VERIFYING, _I.ESCALATED}),
    # A verification that fails goes back to investigating.
    _I.VERIFYING: frozenset({_I.RESOLVED, _I.INVESTIGATING, _I.ESCALATED}),
    _I.ESCALATED: frozenset({_I.TRIAGED, _I.INVESTIGATING, _I.VERIFYING}),
    _I.RESOLVED: frozenset(),
}


def check_incident_transition(current: IncidentState, target: IncidentState) -> None:
    check_transition("incident", INCIDENT_TRANSITIONS, current, target)


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Cause(StrEnum):
    """Root causes Phase 6 detects (more arrive with later phases)."""

    TRACKING_MISSING = "tracking_missing"  # past the SLA window without tracking (6.2)
    SYNC_DRIFT = "sync_drift"  # reconciliation found the store and Etsy apart (5.5)
    TRACKING_MISMATCH = "tracking_mismatch"  # the relay's re-read disagrees (6.10)
    AUTOMATION_DEAD_LETTERED = "automation_dead_lettered"  # an automation gave up (6.7)


class ResourceKind(StrEnum):
    ORDER = "order"
    AUTOMATION = "automation"


DEFAULT_SEVERITY: Mapping[Cause, Severity] = {
    Cause.TRACKING_MISSING: Severity.MEDIUM,
    Cause.SYNC_DRIFT: Severity.LOW,
    Cause.TRACKING_MISMATCH: Severity.HIGH,
    Cause.AUTOMATION_DEAD_LETTERED: Severity.HIGH,
}


def dedup_key(cause: Cause, resource: ResourceKind, resource_id: str) -> str:
    if not resource_id or len(resource_id) > 120 or ":" in resource_id:  # noqa: PLR2004 - key size
        raise ValueError("a resource ID is 1..120 characters without a colon")
    return f"{cause.value}:{resource.value}:{resource_id}"


class EvidenceKind(StrEnum):
    """Verified facts that close an incident (FR-INC-05): never an agent's statement."""

    TRACKING_PRESENT = "tracking_present"  # a tracking event recorded from Etsy's receipt
    ORDER_CLOSED = "order_closed"  # the order was cancelled or refunded
    DRIFT_CLEARED = "drift_cleared"  # a later reconciliation found Etsy and the store equal


@dataclass(frozen=True)
class Evidence:
    """A fact with where it came from; `reference` is an identifier, never buyer data."""

    kind: EvidenceKind
    observed_at: datetime
    source: str  # "etsy_receipt", "reconciliation", ...
    reference: str  # for example a tracking event ID

    def __post_init__(self) -> None:
        if not self.source or not self.reference:
            raise ValueError("evidence names its source and reference")


class ResolutionRefused(Exception):  # noqa: N818 - a domain error named for what happened
    """An incident may resolve only with evidence (AC-6.3.2)."""


def resolution_path(current: IncidentState, evidence: Evidence | None) -> tuple[IncidentState, ...]:
    """The states an incident passes when evidence arrives: to `VERIFYING` (directly, as every
    open state allows) and then `RESOLVED`. Raises without evidence or when already resolved."""
    if evidence is None:
        raise ResolutionRefused("an incident resolves on verified evidence only")
    if current is _I.RESOLVED:
        raise IllegalTransition("incident", current, _I.RESOLVED)
    if current is _I.VERIFYING:
        return (_I.RESOLVED,)
    if current is _I.REMEDIATION_PROPOSED:
        # Its only way to verify is through a remedy; the evidence shows the remedy happened
        # outside HTCO, so the record says a person handled it.
        return (_I.DEVELOPER_REQUIRED, _I.VERIFYING, _I.RESOLVED)
    return (_I.VERIFYING, _I.RESOLVED)


# --- Escalation by severity and age (slice 6.7) --------------------------------------------------

ESCALATE_AFTER: Mapping[Severity, timedelta] = {
    Severity.LOW: timedelta(days=7),
    Severity.MEDIUM: timedelta(days=3),
    Severity.HIGH: timedelta(hours=24),
    Severity.CRITICAL: timedelta(hours=4),
}
_ESCALATABLE = frozenset({_I.DETECTED, _I.TRIAGED, _I.INVESTIGATING})


def should_escalate(
    state: IncidentState, severity: Severity, *, detected_at: datetime, now: datetime
) -> bool:
    """An incident nobody took further than investigating escalates once it is older than its
    severity allows."""
    return state in _ESCALATABLE and now - detected_at > ESCALATE_AFTER[severity]
