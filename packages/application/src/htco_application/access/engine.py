"""Policy engine v1 (slice 7.2): one decision for every actor kind, deny by default.

`evaluate` is pure. It answers `ALLOW`, `REQUIRE_APPROVAL` or `DENY` with a reason, under one
version of the organization's policy (`htco_domain.policy`), in this order:

1. Without a policy every actor is denied. An `advisor` is denied everything (ADR-0015,
   AC-7.2.5); so is any actor when the capability is not in the policy's catalogue (AC-7.2.1),
   or when the policy or the shop belongs to another organization.
2. A `user` is decided by the role matrix of v0 (`access.policy`, PERMISSION_MATRIX section 1):
   an `A` cell asks for an approval, MFA and step-up deny with their reason.
3. An `employee`, an `automation` or a `system` component uses only its own grants in the policy,
   never a user's (FR-IAM-07, AC-7.2.4). An automation or a system component acts under its
   service identity on any shop of its organization. An employee is narrowed by the originating
   user's shop access, and without an originating user it reaches no shop.
4. For those identities the capability's side effect and risk decide last (ADR-0014, ER-AI-04):
   an L0 employee only reads; a critical capability always needs an approval; an external or
   financial one needs an approval unless the policy pre-approves it for that identity (an L3
   employee, an automation or a system component).

Every decision also says what it reaches (`PolicyDecision.shops` and `masked`, quill2 on #189):
a request on a shop reaches that shop; one without a shop (a list) reaches what the identity may
list. For a user that is v0's scope (every shop for an organization-wide cell, the granted shops
for an `S` cell) with v0's masking; for an employee, what the originating user reaches (every
shop for an Owner or Admin, the granted shops otherwise, none without an originating user); for
an automation or a system component, every shop of its organization. A denial reaches nothing.

`baseline_policy` builds an organization's first policy from the registry. The engine does not
yet replace the Action Gateway's own checks in `execution/` (follow-up, see the feature docs).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from htco_application.access.policy import (
    PERMISSIONS,
    Capability,
    Outcome,
    Principal,
    Scope,
    ShopRef,
    decide,
)
from htco_application.workforce.registry import Registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role
from htco_domain.policy import (
    ActorGrants,
    CapabilitySpec,
    PolicyDecision,
    PolicyDocument,
    PolicyVersion,
    Reason,
    Verdict,
)
from htco_domain.tooling import RiskLevel, SideEffect
from htco_domain.workforce import PRODUCTION_READINESS, AutonomyLevel

_N, _I, _X = SideEffect.NONE, SideEffect.INTERNAL, SideEffect.EXTERNAL
_LOW, _MED, _HIGH = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH

# The side effect and risk of each human capability (PERMISSION_MATRIX section 1).
_HUMAN: dict[Capability, CapabilitySpec] = {
    Capability.VIEW_OVERVIEW: CapabilitySpec(_N, _LOW),
    Capability.MANAGE_ACCESS: CapabilitySpec(_I, _HIGH),
    Capability.CONNECT_SHOP: CapabilitySpec(_X, _HIGH),
    Capability.VIEW_ORDERS: CapabilitySpec(_N, _LOW),
    Capability.VIEW_BUYER_CONTACT: CapabilitySpec(_N, _MED),
    Capability.VIEW_INCIDENTS: CapabilitySpec(_N, _LOW),
    Capability.UPDATE_INCIDENTS: CapabilitySpec(_I, _LOW),
    Capability.RETRY_SYNC: CapabilitySpec(_I, _LOW),
    Capability.CONFIRM_SHIPMENT_WRITE: CapabilitySpec(_X, _HIGH),
    Capability.REQUEST_BULK_RESYNC: CapabilitySpec(_I, _MED),
    Capability.VIEW_TASKS: CapabilitySpec(_N, _LOW),
    Capability.DECIDE_LOW_RISK_APPROVAL: CapabilitySpec(_I, _MED),
    Capability.DECIDE_HIGH_RISK_APPROVAL: CapabilitySpec(_I, _HIGH),
    Capability.VIEW_FINANCE: CapabilitySpec(_N, _MED),
    Capability.EDIT_MODEL_POLICIES: CapabilitySpec(_I, _HIGH),
    Capability.EDIT_PRODUCT_FACTS: CapabilitySpec(_I, _LOW),
    Capability.RUN_LISTING_HEALTH_AUDIT: CapabilitySpec(_I, _LOW),
    Capability.VIEW_AGENT_DIRECTORY: CapabilitySpec(_N, _LOW),
    Capability.VIEW_AGENT_RUNS: CapabilitySpec(_N, _LOW),
    Capability.VIEW_AUDIT_LOG: CapabilitySpec(_N, _MED),
    Capability.VIEW_BROWSER_ENVIRONMENT: CapabilitySpec(_N, _LOW),
}
HUMAN_CAPABILITIES: Mapping[Capability, CapabilitySpec] = MappingProxyType(_HUMAN)

# Roles that reach every shop of their organization (the `✓` columns of the matrix).
_ORGANIZATION_ROLES = frozenset({Role.OWNER, Role.ADMIN})


@dataclass(frozen=True)
class PolicyRequest:
    """One question to the engine. `organization_id`, `actor` and `shop` come from the run or
    the authenticated context, never from a model (ER-AI-12)."""

    organization_id: uuid.UUID
    actor: ActorRef
    capability: str
    shop: ShopRef | None
    now: datetime
    # The signed-in user for a `user` actor; the originating user of an employee's request.
    # Never read for an automation, an advisor or a system component.
    principal: Principal | None = None


def evaluate(policy: PolicyVersion | None, request: PolicyRequest) -> PolicyDecision:
    if request.now.tzinfo is None:
        raise ValueError("evaluate needs a timezone-aware clock")
    version = None if policy is None else policy.version
    reason = _reason(policy, request)
    if reason.verdict is Verdict.DENY:
        return PolicyDecision(reason, request.capability, request.actor, version)
    shops, masked = _reach(request)
    return PolicyDecision(
        reason, request.capability, request.actor, version, shops=shops, masked=masked
    )


def _reach(request: PolicyRequest) -> tuple[frozenset[uuid.UUID] | None, bool]:
    """What an allowed or approvable request reaches: its shop, or without one what the
    identity may list, and whether buyer details stay masked."""
    principal = request.principal
    masked = False
    if request.actor.kind is ActorKind.USER and principal is not None:
        # Reached only once `_user` allowed it: the capability is a human one.
        decision = decide(
            principal, Capability(request.capability), shop=request.shop, now=request.now
        )
        masked = decision.masked
        if request.shop is None:
            reaches = None if decision.scope is Scope.ORGANIZATION else principal.granted_shops
            return reaches, masked
    if request.shop is not None:
        return frozenset({request.shop.id}), masked
    if request.actor.kind is ActorKind.EMPLOYEE:
        if principal is None:
            return frozenset(), False
        if principal.role in _ORGANIZATION_ROLES:
            return None, False
        return principal.granted_shops, False
    return None, False  # an automation or a system component: every shop of its organization


def _reason(policy: PolicyVersion | None, request: PolicyRequest) -> Reason:  # noqa: PLR0911 - one return per rule, in order
    if policy is None:
        return Reason.NO_POLICY
    if request.actor.kind is ActorKind.ADVISOR:
        return Reason.ADVISOR_HAS_NO_CAPABILITIES
    shop = request.shop
    if policy.organization_id != request.organization_id or (
        shop is not None and shop.organization_id != request.organization_id
    ):
        return Reason.OTHER_ORGANIZATION
    spec = policy.document.capabilities.get(request.capability)
    if spec is None:
        return Reason.UNKNOWN_CAPABILITY
    if request.actor.kind is ActorKind.USER:
        return _user(request)
    grants = _grants_of(policy.document, request.actor)
    if grants is None:
        return Reason.UNKNOWN_ACTOR
    if request.capability not in grants.capabilities:
        return Reason.NOT_GRANTED
    if request.actor.kind is ActorKind.EMPLOYEE:
        narrowed = _originating_user_reaches(request.principal, request.organization_id, shop)
        if narrowed is not None:
            return narrowed
    return _by_risk(request.capability, spec, grants)


def _user(request: PolicyRequest) -> Reason:
    principal = request.principal
    if principal is None or principal.organization_id != request.organization_id:
        return Reason.UNKNOWN_ACTOR
    try:
        capability = Capability(request.capability)
    except ValueError:
        return Reason.NOT_GRANTED  # a capability of employees, automations or the system
    decision = decide(principal, capability, shop=request.shop, now=request.now)
    match decision.outcome:
        case Outcome.MFA_REQUIRED:
            return Reason.MFA_REQUIRED
        case Outcome.STEP_UP_REQUIRED:
            return Reason.STEP_UP_REQUIRED
        case Outcome.DENY:
            # The shop is of this organization (checked first), so a role holding the
            # capability was denied only the shop.
            held = principal.role in PERMISSIONS[capability]
            return Reason.SHOP_NOT_GRANTED if held else Reason.NOT_GRANTED
        case Outcome.ALLOW:
            return Reason.ROLE_REQUIRES_APPROVAL if decision.via_approval else Reason.GRANTED


def _grants_of(document: PolicyDocument, actor: ActorRef) -> ActorGrants | None:
    sections = {
        ActorKind.EMPLOYEE: document.employees,
        ActorKind.AUTOMATION: document.automations,
        ActorKind.SYSTEM: document.systems,
    }
    # A kind without a section has no grants: the caller answers UNKNOWN_ACTOR (sage, #189).
    section = sections.get(actor.kind)
    return None if section is None else section.get(actor.id)


def _originating_user_reaches(
    principal: Principal | None, organization_id: uuid.UUID, shop: ShopRef | None
) -> Reason | None:
    """Why the originating user's shop access stops an employee, or None when it reaches. Without
    a shop only the user's organization is checked; `_reach` then narrows the list."""
    if shop is None:
        if principal is not None and principal.organization_id != organization_id:
            return Reason.OTHER_ORGANIZATION
        return None
    if principal is None:
        return Reason.NO_ORIGINATING_USER
    if principal.organization_id != organization_id:
        return Reason.OTHER_ORGANIZATION
    if principal.role in _ORGANIZATION_ROLES or shop.id in principal.granted_shops:
        return None
    return Reason.SHOP_NOT_GRANTED


def _by_risk(capability: str, spec: CapabilitySpec, grants: ActorGrants) -> Reason:
    if grants.autonomy is AutonomyLevel.L0 and spec.side_effect is not SideEffect.NONE:
        return Reason.AUTONOMY_TOO_LOW  # L0 suggests only
    if spec.risk is RiskLevel.CRITICAL:
        return Reason.CRITICAL_RISK
    if spec.acts_outside:
        if capability in grants.pre_approved:
            return Reason.PRE_APPROVED
        return Reason.ACTS_OUTSIDE_HTCO
    return Reason.GRANTED


def baseline_policy(registry: Registry) -> PolicyDocument:
    """An organization's first policy: the human capabilities and every registry tool in the
    catalogue; each production-ready automation with its `allowed_actions`; each production-ready
    employee with its `allowed_tools` and autonomy level. No pre-approvals and no system grants:
    those are added only by a reviewed policy change."""
    capabilities = {capability.value: spec for capability, spec in HUMAN_CAPABILITIES.items()}
    capabilities |= {
        name: CapabilitySpec(tool.side_effect, tool.risk) for name, tool in registry.tools.items()
    }
    return PolicyDocument(
        capabilities=capabilities,
        employees={
            employee_id: ActorGrants(profile.allowed_tools, autonomy=profile.autonomy)
            for employee_id, profile in registry.employees.items()
            if profile.is_production_ready()
        },
        automations={
            automation_id: ActorGrants(automation.allowed_actions)
            for automation_id, automation in registry.automations.items()
            if automation.readiness in PRODUCTION_READINESS
        },
    )
