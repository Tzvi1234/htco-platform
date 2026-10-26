"""Policy engine v1 values (slice 7.2): versioned policies and the decisions they produce.

A policy is one organization's reviewed document, immutable once published: changing it
publishes the next version (AC-7.2.3). It holds the capability catalogue, each capability with
its side effect and risk, and the grants of the non-human identities (FR-IAM-07): employees with
their autonomy level, automations and system components with their service identities. Human
roles keep their matrix in code (`htco_application.access.policy`). There is no advisor section:
a policy cannot grant a Decision Advisor anything (ADR-0015, AC-7.2.5).

A decision is a reason, and the reason fixes the verdict, so an ALLOW without a granting reason
cannot be built. Every decision names the policy version it was made under.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from htco_domain.actors import ActorRef
from htco_domain.tooling import RiskLevel, SideEffect
from htco_domain.workforce import AutonomyLevel

MAX_NAME_LENGTH = 100
MAX_ACTOR_ID_LENGTH = 200
_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class Reason(StrEnum):
    # ALLOW
    GRANTED = "granted"
    PRE_APPROVED = "pre_approved"  # an external capability a written policy pre-approves (ER-AI-04)
    # REQUIRE_APPROVAL
    ROLE_REQUIRES_APPROVAL = "role_requires_approval"  # an `A` cell of the human matrix
    ACTS_OUTSIDE_HTCO = "acts_outside_htco"  # external or financial, not pre-approved
    CRITICAL_RISK = "critical_risk"  # always an approval
    # DENY
    NO_POLICY = "no_policy"
    UNKNOWN_CAPABILITY = "unknown_capability"
    UNKNOWN_ACTOR = "unknown_actor"
    ADVISOR_HAS_NO_CAPABILITIES = "advisor_has_no_capabilities"
    NOT_GRANTED = "not_granted"
    OTHER_ORGANIZATION = "other_organization"
    SHOP_NOT_GRANTED = "shop_not_granted"
    NO_ORIGINATING_USER = "no_originating_user"
    AUTONOMY_TOO_LOW = "autonomy_too_low"
    MFA_REQUIRED = "mfa_required"
    STEP_UP_REQUIRED = "step_up_required"

    @property
    def verdict(self) -> Verdict:
        if self in _ALLOWING:
            return Verdict.ALLOW
        if self in _APPROVING:
            return Verdict.REQUIRE_APPROVAL
        return Verdict.DENY


_ALLOWING = frozenset({Reason.GRANTED, Reason.PRE_APPROVED})
_APPROVING = frozenset(
    {Reason.ROLE_REQUIRES_APPROVAL, Reason.ACTS_OUTSIDE_HTCO, Reason.CRITICAL_RISK}
)


@dataclass(frozen=True)
class CapabilitySpec:
    side_effect: SideEffect
    risk: RiskLevel

    def __post_init__(self) -> None:
        if self.side_effect in {SideEffect.EXTERNAL, SideEffect.FINANCIAL} and (
            self.risk is RiskLevel.LOW
        ):
            raise ValueError("a capability that acts outside HTCO is at least medium risk")

    @property
    def acts_outside(self) -> bool:
        return self.side_effect in {SideEffect.EXTERNAL, SideEffect.FINANCIAL}


@dataclass(frozen=True)
class ActorGrants:
    """One identity's permission set: its own, never a user's (FR-IAM-07)."""

    capabilities: frozenset[str]
    # External capabilities this identity may use without an approval (ER-AI-04).
    pre_approved: frozenset[str] = frozenset()
    autonomy: AutonomyLevel | None = None  # employees only

    def __post_init__(self) -> None:
        if not self.pre_approved <= self.capabilities:
            raise ValueError("every pre-approved capability must also be granted")


def _check_actor_id(actor_id: str) -> None:
    if not actor_id.strip() or len(actor_id) > MAX_ACTOR_ID_LENGTH:
        raise ValueError(f"invalid actor id {actor_id[:20]!r}")


def _check_grants(
    section: str, grants: Mapping[str, ActorGrants], catalogue: Mapping[str, CapabilitySpec]
) -> None:
    for actor_id, grant in grants.items():
        _check_actor_id(actor_id)
        unknown = sorted(grant.capabilities - catalogue.keys())
        if unknown:
            raise ValueError(f"{section} {actor_id!r}: unknown capability {unknown[0]!r}")
        is_employee = section == "employees"
        if is_employee != (grant.autonomy is not None):
            raise ValueError(f"{section} {actor_id!r}: only employees have an autonomy level")
        if is_employee and grant.pre_approved and grant.autonomy is not AutonomyLevel.L3:
            raise ValueError(f"employee {actor_id!r}: only an L3 employee holds pre-approvals")


@dataclass(frozen=True)
class PolicyDocument:
    capabilities: Mapping[str, CapabilitySpec]
    employees: Mapping[str, ActorGrants] = field(default_factory=dict)
    automations: Mapping[str, ActorGrants] = field(default_factory=dict)
    systems: Mapping[str, ActorGrants] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.capabilities:
            if len(name) > MAX_NAME_LENGTH or not _NAME.match(name):
                raise ValueError(f"invalid capability name {name[:20]!r}")
        for section in ("employees", "automations", "systems"):
            _check_grants(section, getattr(self, section), self.capabilities)
        # Read-only views: a published policy never changes in place.
        for section in ("capabilities", "employees", "automations", "systems"):
            object.__setattr__(self, section, MappingProxyType(dict(getattr(self, section))))

    def to_json(self) -> dict[str, Any]:
        def grants(section: Mapping[str, ActorGrants]) -> dict[str, Any]:
            return {
                actor_id: {
                    "capabilities": sorted(grant.capabilities),
                    "pre_approved": sorted(grant.pre_approved),
                    "autonomy": None if grant.autonomy is None else grant.autonomy.name,
                }
                for actor_id, grant in sorted(section.items())
            }

        return {
            "capabilities": {
                name: {"side_effect": spec.side_effect.value, "risk": spec.risk.value}
                for name, spec in sorted(self.capabilities.items())
            },
            "employees": grants(self.employees),
            "automations": grants(self.automations),
            "systems": grants(self.systems),
        }

    @classmethod
    def from_json(cls, data: object) -> PolicyDocument:
        """Parse a stored document strictly: unknown or missing keys are rejected."""
        try:
            return _parse_document(data)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid policy document: {error}") from None


_DOCUMENT_KEYS = frozenset({"capabilities", "employees", "automations", "systems"})
_SPEC_KEYS = frozenset({"side_effect", "risk"})
_GRANT_KEYS = frozenset({"capabilities", "pre_approved", "autonomy"})


def _object(value: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError(f"expected exactly the keys {sorted(keys)}")
    return value  # pyright: ignore[reportUnknownVariableType]


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected an object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _names(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):  # pyright: ignore[reportUnknownVariableType]
        raise TypeError("expected a list of names")
    return frozenset(value)  # pyright: ignore[reportUnknownArgumentType]


def _parse_grants(value: object) -> dict[str, ActorGrants]:
    parsed: dict[str, ActorGrants] = {}
    for actor_id, raw in _mapping(value).items():
        grant = _object(raw, _GRANT_KEYS)
        autonomy = grant["autonomy"]
        parsed[actor_id] = ActorGrants(
            capabilities=_names(grant["capabilities"]),
            pre_approved=_names(grant["pre_approved"]),
            autonomy=None if autonomy is None else AutonomyLevel[autonomy],
        )
    return parsed


def _parse_document(data: object) -> PolicyDocument:
    document = _object(data, _DOCUMENT_KEYS)
    capabilities: dict[str, CapabilitySpec] = {}
    for name, raw in _mapping(document["capabilities"]).items():
        spec = _object(raw, _SPEC_KEYS)
        capabilities[name] = CapabilitySpec(
            SideEffect(spec["side_effect"]), RiskLevel(spec["risk"])
        )
    return PolicyDocument(
        capabilities=capabilities,
        employees=_parse_grants(document["employees"]),
        automations=_parse_grants(document["automations"]),
        systems=_parse_grants(document["systems"]),
    )


@dataclass(frozen=True)
class PolicyVersion:
    """One published version of an organization's policy (a row of `app.policies`)."""

    organization_id: uuid.UUID
    version: int
    document: PolicyDocument
    created_by: ActorRef
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("a policy version starts at 1")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at needs a timezone")


@dataclass(frozen=True)
class PolicyDecision:
    reason: Reason
    capability: str
    actor: ActorRef
    # The version decided under; None only when the organization has no policy yet.
    policy_version: int | None

    def __post_init__(self) -> None:
        if (self.policy_version is None) != (self.reason is Reason.NO_POLICY):
            raise ValueError("a decision names its policy version unless there is no policy")
        if self.policy_version is not None and self.policy_version < 1:
            raise ValueError("a policy version starts at 1")

    @property
    def verdict(self) -> Verdict:
        return self.reason.verdict
