"""Policy engine v1 (slice 7.2): one decision for every actor kind, deny by default.

Users keep the role matrix of v0 (PERMISSION_MATRIX section 1, parsed from the document by
`test_access_policy`); employees, automations and system components are evaluated with their own
grants from the organization's versioned policy, never a user's (FR-IAM-07); an advisor has no
capabilities at all (ADR-0015).
"""

import dataclasses
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from htco_application.access.engine import (
    HUMAN_CAPABILITIES,
    PolicyRequest,
    baseline_policy,
    evaluate,
)
from htco_application.access.policy import STEP_UP_WINDOW, Capability, Principal, ShopRef
from htco_application.workforce.files import read_registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.policy import (
    ActorGrants,
    CapabilitySpec,
    PolicyDocument,
    PolicyVersion,
    Reason,
    Verdict,
)
from htco_domain.tooling import RiskLevel, SideEffect
from htco_domain.workforce import (
    PRODUCTION_READINESS,
    AutonomyLevel,
    EmployeeProfile,
    EvaluationResult,
    Readiness,
    SectionStatus,
    SpecSection,
)
from tests.application.test_access_policy import (
    DOCUMENTED,
    FOREIGN_SHOP,
    NOW,
    ORG,
    OTHER_ORG,
    ROLE_COLUMNS,
    SHOP_A,
    SHOP_B,
    a_principal,
)

REGISTRY = Path(__file__).resolve().parents[2] / "registry"

READ = CapabilitySpec(SideEffect.NONE, RiskLevel.LOW)
INTERNAL = CapabilitySpec(SideEffect.INTERNAL, RiskLevel.MEDIUM)
EXTERNAL = CapabilitySpec(SideEffect.EXTERNAL, RiskLevel.HIGH)
FINANCIAL = CapabilitySpec(SideEffect.FINANCIAL, RiskLevel.HIGH)
CRITICAL = CapabilitySpec(SideEffect.INTERNAL, RiskLevel.CRITICAL)
SPECS = {
    "orders.read": READ,
    "tasks.update": INTERNAL,
    "etsy.tracking.upload": EXTERNAL,
    "finance.pay": FINANCIAL,
    "registry.change": CRITICAL,
}
EVERYTHING = frozenset(SPECS)
OUTSIDE = frozenset({"etsy.tracking.upload", "finance.pay"})


def employee(level: AutonomyLevel, *, pre_approved: frozenset[str] = frozenset()) -> ActorGrants:
    return ActorGrants(EVERYTHING, pre_approved=pre_approved, autonomy=level)


DOCUMENT = PolicyDocument(
    capabilities={**SPECS, **{c.value: spec for c, spec in HUMAN_CAPABILITIES.items()}},
    employees={
        "gal": employee(AutonomyLevel.L0),
        "maya": employee(AutonomyLevel.L1),
        "tom": employee(AutonomyLevel.L2),
        "dana": employee(AutonomyLevel.L3),
        "dana-pre": employee(AutonomyLevel.L3, pre_approved=OUTSIDE),
        "noa": ActorGrants(frozenset({"orders.read"}), autonomy=AutonomyLevel.L1),
    },
    automations={
        "tracking-sync": ActorGrants(EVERYTHING),
        "relay": ActorGrants(EVERYTHING, pre_approved=OUTSIDE),
        "order-intake": ActorGrants(frozenset({"orders.read"})),
    },
    systems={
        "outbox": ActorGrants(EVERYTHING),
        "outbox-pre": ActorGrants(EVERYTHING, pre_approved=OUTSIDE),
    },
)
POLICY = PolicyVersion(ORG, 4, DOCUMENT, ActorRef(ActorKind.SYSTEM, "test"), NOW)


def ask(  # noqa: PLR0913 - one argument per field of the request
    kind: ActorKind,
    actor_id: str,
    capability: str,
    *,
    shop: ShopRef | None = SHOP_A,
    principal: Principal | None = None,
    policy: PolicyVersion | None = POLICY,
    organization_id: uuid.UUID = ORG,
    now: datetime = NOW,
) -> Reason:
    request = PolicyRequest(
        organization_id=organization_id,
        actor=ActorRef(kind, actor_id),
        capability=capability,
        shop=shop,
        now=now,
        principal=principal,
    )
    decision = evaluate(policy, request)
    assert decision.capability == capability
    assert decision.actor == request.actor
    assert decision.policy_version == (None if policy is None else policy.version)
    return decision.reason


# --- AC-7.2.2: users, every row and column of PERMISSION_MATRIX section 1 -----------------


@pytest.mark.ac("AC-7.2.2")
@pytest.mark.parametrize("role", ROLE_COLUMNS)
@pytest.mark.parametrize("capability", list(Capability))
def test_user__every_cell_of_the_human_matrix(role: Role, capability: Capability) -> None:
    reason = ask(ActorKind.USER, "u-1", capability.value, principal=a_principal(role))

    grant = DOCUMENTED[capability].get(role)
    if grant is None:
        assert reason is Reason.NOT_GRANTED
    elif grant.via_approval:
        assert reason is Reason.ROLE_REQUIRES_APPROVAL
    else:
        assert reason is Reason.GRANTED


def test_user__a_shop_role_is_denied_a_shop_it_was_not_granted() -> None:
    reason = ask(
        ActorKind.USER, "u-1", "view_orders", shop=SHOP_B, principal=a_principal(Role.SUPPORT)
    )

    assert reason is Reason.SHOP_NOT_GRANTED


def test_user__mfa_and_step_up_deny_with_their_reason() -> None:
    aal1 = a_principal(Role.OWNER, assurance=AssuranceLevel.AAL1)
    stale = a_principal(Role.OWNER, reauthenticated_at=NOW - STEP_UP_WINDOW)

    assert ask(ActorKind.USER, "u-1", "manage_access", principal=aal1) is Reason.MFA_REQUIRED
    assert ask(ActorKind.USER, "u-1", "connect_shop", principal=stale) is Reason.STEP_UP_REQUIRED
    assert Reason.STEP_UP_REQUIRED.verdict is Verdict.DENY


def test_user__a_capability_of_another_actor_kind_is_not_granted() -> None:
    reason = ask(ActorKind.USER, "u-1", "orders.read", principal=a_principal(Role.OWNER))

    assert reason is Reason.NOT_GRANTED


def test_user__of_another_organization_is_an_unknown_actor() -> None:
    foreign = Principal(
        organization_id=OTHER_ORG,
        role=Role.OWNER,
        assurance=AssuranceLevel.AAL2,
        reauthenticated_at=NOW,
        granted_shops=frozenset(),
    )

    assert ask(ActorKind.USER, "u-1", "view_orders", principal=foreign) is Reason.UNKNOWN_ACTOR


# --- AC-7.2.2: employees, automations and system identities, by autonomy and risk ---------

E, A, S = ActorKind.EMPLOYEE, ActorKind.AUTOMATION, ActorKind.SYSTEM
TABLE = [
    # actor kind, id, capability, expected
    (E, "gal", "orders.read", Reason.GRANTED),
    (E, "gal", "tasks.update", Reason.AUTONOMY_TOO_LOW),
    (E, "gal", "etsy.tracking.upload", Reason.AUTONOMY_TOO_LOW),
    (E, "gal", "finance.pay", Reason.AUTONOMY_TOO_LOW),
    (E, "gal", "registry.change", Reason.AUTONOMY_TOO_LOW),
    (E, "maya", "orders.read", Reason.GRANTED),
    (E, "maya", "tasks.update", Reason.GRANTED),
    (E, "maya", "etsy.tracking.upload", Reason.ACTS_OUTSIDE_HTCO),
    (E, "maya", "finance.pay", Reason.ACTS_OUTSIDE_HTCO),
    (E, "maya", "registry.change", Reason.CRITICAL_RISK),
    (E, "tom", "orders.read", Reason.GRANTED),
    (E, "tom", "tasks.update", Reason.GRANTED),
    (E, "tom", "etsy.tracking.upload", Reason.ACTS_OUTSIDE_HTCO),
    (E, "tom", "finance.pay", Reason.ACTS_OUTSIDE_HTCO),
    (E, "tom", "registry.change", Reason.CRITICAL_RISK),
    (E, "dana", "tasks.update", Reason.GRANTED),
    (E, "dana", "etsy.tracking.upload", Reason.ACTS_OUTSIDE_HTCO),
    (E, "dana", "registry.change", Reason.CRITICAL_RISK),
    (E, "dana-pre", "etsy.tracking.upload", Reason.PRE_APPROVED),
    (E, "dana-pre", "finance.pay", Reason.PRE_APPROVED),
    (E, "dana-pre", "registry.change", Reason.CRITICAL_RISK),
    (E, "noa", "orders.read", Reason.GRANTED),
    (E, "noa", "tasks.update", Reason.NOT_GRANTED),
    (E, "noa", "view_orders", Reason.NOT_GRANTED),
    (A, "tracking-sync", "orders.read", Reason.GRANTED),
    (A, "tracking-sync", "tasks.update", Reason.GRANTED),
    (A, "tracking-sync", "etsy.tracking.upload", Reason.ACTS_OUTSIDE_HTCO),
    (A, "tracking-sync", "finance.pay", Reason.ACTS_OUTSIDE_HTCO),
    (A, "tracking-sync", "registry.change", Reason.CRITICAL_RISK),
    (A, "relay", "etsy.tracking.upload", Reason.PRE_APPROVED),
    (A, "relay", "registry.change", Reason.CRITICAL_RISK),
    (A, "order-intake", "orders.read", Reason.GRANTED),
    (A, "order-intake", "tasks.update", Reason.NOT_GRANTED),
    (S, "outbox", "tasks.update", Reason.GRANTED),
    (S, "outbox", "etsy.tracking.upload", Reason.ACTS_OUTSIDE_HTCO),
    (S, "outbox-pre", "etsy.tracking.upload", Reason.PRE_APPROVED),
    (S, "outbox-pre", "registry.change", Reason.CRITICAL_RISK),
]


@pytest.mark.ac("AC-7.2.2")
@pytest.mark.parametrize(("kind", "actor_id", "capability", "expected"), TABLE)
def test_identity__the_permission_table(
    kind: ActorKind, actor_id: str, capability: str, expected: Reason
) -> None:
    principal = a_principal(Role.OWNER) if kind is ActorKind.EMPLOYEE else None

    assert ask(kind, actor_id, capability, principal=principal) is expected


@pytest.mark.ac("AC-7.2.2")
def test_identity__the_table_covers_every_level_and_every_kind_of_capability() -> None:
    employees = {row[1] for row in TABLE if row[0] is ActorKind.EMPLOYEE}
    levels = {DOCUMENT.employees[e].autonomy for e in employees}
    side_effects = {SPECS[row[2]].side_effect for row in TABLE if row[2] in SPECS}

    assert levels == set(AutonomyLevel)
    assert side_effects == set(SideEffect)
    assert {row[0] for row in TABLE} == {ActorKind.EMPLOYEE, ActorKind.AUTOMATION, ActorKind.SYSTEM}
    assert {row[3] for row in TABLE} >= {
        Reason.GRANTED,
        Reason.PRE_APPROVED,
        Reason.ACTS_OUTSIDE_HTCO,
        Reason.CRITICAL_RISK,
        Reason.AUTONOMY_TOO_LOW,
        Reason.NOT_GRANTED,
    }


# --- AC-7.2.1: unknown capability or actor -----------------------------------------------


@pytest.mark.ac("AC-7.2.1")
@pytest.mark.parametrize(
    ("kind", "actor_id"),
    [
        (ActorKind.USER, "u-1"),
        (ActorKind.EMPLOYEE, "dana-pre"),
        (ActorKind.AUTOMATION, "relay"),
        (ActorKind.SYSTEM, "outbox-pre"),
    ],
)
@pytest.mark.parametrize("capability", ["orders.delete", "", "VIEW_ORDERS", "view-orders"])
def test_unknown_capability__is_denied(kind: ActorKind, actor_id: str, capability: str) -> None:
    reason = ask(kind, actor_id, capability, principal=a_principal(Role.OWNER))

    assert reason is Reason.UNKNOWN_CAPABILITY
    assert reason.verdict is Verdict.DENY


@pytest.mark.ac("AC-7.2.1")
@pytest.mark.parametrize("kind", [ActorKind.EMPLOYEE, ActorKind.AUTOMATION, ActorKind.SYSTEM])
def test_unknown_actor__is_denied(kind: ActorKind) -> None:
    reason = ask(kind, "nobody", "orders.read", principal=a_principal(Role.OWNER))

    assert reason is Reason.UNKNOWN_ACTOR


@pytest.mark.ac("AC-7.2.1")
def test_user_without_a_principal__is_an_unknown_actor() -> None:
    assert ask(ActorKind.USER, "u-1", "view_orders") is Reason.UNKNOWN_ACTOR


@pytest.mark.ac("AC-7.2.1")
@pytest.mark.parametrize("kind", list(ActorKind))
def test_no_policy__denies_every_actor(kind: ActorKind) -> None:
    reason = ask(
        kind, "tracking-sync", "orders.read", principal=a_principal(Role.OWNER), policy=None
    )

    assert reason.verdict is Verdict.DENY


def test_no_policy__names_no_version() -> None:
    reason = ask(ActorKind.AUTOMATION, "tracking-sync", "orders.read", policy=None)

    assert reason is Reason.NO_POLICY


# --- AC-7.2.4: own permission sets; an employee is narrowed by the originating user ------


@pytest.mark.ac("AC-7.2.4")
@pytest.mark.parametrize("capability", ["tasks.update", "view_buyer_contact", "manage_access"])
def test_automation__an_owner_attached_to_the_request_never_widens_its_grants(
    capability: str,
) -> None:
    reason = ask(
        ActorKind.AUTOMATION, "order-intake", capability, principal=a_principal(Role.OWNER)
    )

    assert reason is Reason.NOT_GRANTED


@pytest.mark.ac("AC-7.2.4")
def test_automation__acts_on_any_shop_of_its_organization_without_a_user() -> None:
    assert ask(ActorKind.AUTOMATION, "order-intake", "orders.read", shop=SHOP_B) is Reason.GRANTED


@pytest.mark.ac("AC-7.2.4")
def test_employee__never_gets_a_capability_from_the_originating_user() -> None:
    reason = ask(ActorKind.EMPLOYEE, "noa", "view_buyer_contact", principal=a_principal(Role.OWNER))

    assert reason is Reason.NOT_GRANTED


@pytest.mark.ac("AC-7.2.4")
def test_employee__is_limited_to_the_originating_users_shops() -> None:
    support = a_principal(Role.SUPPORT, shops=frozenset({SHOP_A.id}))

    on_a = ask(ActorKind.EMPLOYEE, "noa", "orders.read", shop=SHOP_A, principal=support)
    on_b = ask(ActorKind.EMPLOYEE, "noa", "orders.read", shop=SHOP_B, principal=support)

    assert on_a is Reason.GRANTED
    assert on_b is Reason.SHOP_NOT_GRANTED


@pytest.mark.ac("AC-7.2.4")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
def test_employee__an_organization_wide_user_lends_every_shop(role: Role) -> None:
    principal = a_principal(role, shops=frozenset())

    assert ask(ActorKind.EMPLOYEE, "noa", "orders.read", shop=SHOP_B, principal=principal) is (
        Reason.GRANTED
    )


@pytest.mark.ac("AC-7.2.4")
def test_employee__without_an_originating_user_reaches_no_shop() -> None:
    assert ask(ActorKind.EMPLOYEE, "noa", "orders.read", shop=SHOP_A) is Reason.NO_ORIGINATING_USER


@pytest.mark.ac("AC-7.2.4")
def test_employee__without_a_shop_is_decided_on_its_own_grants() -> None:
    assert ask(ActorKind.EMPLOYEE, "noa", "orders.read", shop=None) is Reason.GRANTED


@pytest.mark.ac("AC-7.2.4")
def test_employee__an_originating_user_of_another_organization_is_refused() -> None:
    foreign = Principal(
        organization_id=OTHER_ORG,
        role=Role.OWNER,
        assurance=AssuranceLevel.AAL2,
        reauthenticated_at=NOW,
        granted_shops=frozenset({SHOP_A.id}),
    )

    reason = ask(ActorKind.EMPLOYEE, "noa", "orders.read", principal=foreign)

    assert reason is Reason.OTHER_ORGANIZATION


@pytest.mark.ac("AC-7.2.4")
def test_employee__the_narrower_side_wins_even_when_the_user_is_the_owner() -> None:
    # The owner may decide high-risk approvals; the L1 employee acting for them may not even ask
    # for an unapproved external write.
    reason = ask(
        ActorKind.EMPLOYEE, "maya", "etsy.tracking.upload", principal=a_principal(Role.OWNER)
    )

    assert reason is Reason.ACTS_OUTSIDE_HTCO


# --- AC-7.2.5: an advisor authorizes nothing ---------------------------------------------


@pytest.mark.ac("AC-7.2.5")
@pytest.mark.parametrize("capability", [*sorted(DOCUMENT.capabilities), "orders.delete"])
def test_advisor__every_request_is_denied(capability: str) -> None:
    reason = ask(ActorKind.ADVISOR, "jev", capability, principal=a_principal(Role.OWNER))

    assert reason is Reason.ADVISOR_HAS_NO_CAPABILITIES
    assert reason.verdict is Verdict.DENY


@pytest.mark.ac("AC-7.2.5")
def test_advisor__named_like_a_granted_identity_is_still_denied() -> None:
    assert ask(ActorKind.ADVISOR, "tracking-sync", "orders.read") is (
        Reason.ADVISOR_HAS_NO_CAPABILITIES
    )


# --- tenancy ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "actor_id"),
    [
        (ActorKind.USER, "u-1"),
        (ActorKind.EMPLOYEE, "noa"),
        (ActorKind.AUTOMATION, "order-intake"),
        (ActorKind.SYSTEM, "outbox"),
    ],
)
def test_shop_of_another_organization__is_denied_to_every_kind(
    kind: ActorKind, actor_id: str
) -> None:
    principal = a_principal(Role.OWNER, shops=frozenset({SHOP_A.id, FOREIGN_SHOP.id}))
    capability = "view_orders" if kind is ActorKind.USER else "orders.read"

    reason = ask(kind, actor_id, capability, shop=FOREIGN_SHOP, principal=principal)

    assert reason is Reason.OTHER_ORGANIZATION


def test_policy_of_another_organization__is_never_applied() -> None:
    reason = ask(ActorKind.AUTOMATION, "order-intake", "orders.read", organization_id=OTHER_ORG,
                 shop=None)  # fmt: skip

    assert reason is Reason.OTHER_ORGANIZATION


def test_naive_clock__is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ask(ActorKind.AUTOMATION, "order-intake", "orders.read", now=NOW.replace(tzinfo=None))


def test_step_up_window__is_v0s() -> None:
    principal = a_principal(Role.OWNER, reauthenticated_at=NOW - timedelta(minutes=9))

    assert ask(ActorKind.USER, "u-1", "connect_shop", principal=principal) is Reason.GRANTED


# --- the baseline policy from the registry ---------------------------------------------


def test_human_capabilities__every_capability_has_a_side_effect_and_risk() -> None:
    assert set(HUMAN_CAPABILITIES) == set(Capability)


@pytest.mark.parametrize(
    ("capability", "side_effect"),
    [
        (Capability.VIEW_ORDERS, SideEffect.NONE),
        (Capability.CONFIRM_SHIPMENT_WRITE, SideEffect.EXTERNAL),
        (Capability.CONNECT_SHOP, SideEffect.EXTERNAL),
        (Capability.MANAGE_ACCESS, SideEffect.INTERNAL),
    ],
)
def test_human_capabilities__side_effects(capability: Capability, side_effect: SideEffect) -> None:
    assert HUMAN_CAPABILITIES[capability].side_effect is side_effect


def test_human_capabilities__decisions_on_permissions_and_money_are_high_risk() -> None:
    for capability in (
        Capability.MANAGE_ACCESS,
        Capability.CONNECT_SHOP,
        Capability.DECIDE_HIGH_RISK_APPROVAL,
        Capability.EDIT_MODEL_POLICIES,
        Capability.CONFIRM_SHIPMENT_WRITE,
    ):
        assert HUMAN_CAPABILITIES[capability].risk.rank >= RiskLevel.HIGH.rank


def test_baseline__is_built_from_the_registry() -> None:
    registry = read_registry(REGISTRY).registry()

    document = baseline_policy(registry)

    for capability, spec in HUMAN_CAPABILITIES.items():
        assert document.capabilities[capability.value] == spec
    for name, tool in registry.tools.items():
        assert document.capabilities[name] == CapabilitySpec(tool.side_effect, tool.risk)
    ready = {a.id for a in registry.automations.values() if a.readiness in PRODUCTION_READINESS}
    assert set(document.automations) == ready
    for automation_id in ready:
        grants = document.automations[automation_id]
        assert grants.capabilities == registry.automations[automation_id].allowed_actions
        assert grants.pre_approved == frozenset()
    active = {e for e, p in registry.employees.items() if p.is_production_ready()}
    assert set(document.employees) == active
    assert document.systems == {}


def test_baseline__a_production_ready_employee_brings_its_tools_and_level() -> None:
    registry = read_registry(REGISTRY).registry()
    profile = next(iter(registry.employees.values()))
    tool = next(iter(registry.tools))
    ready = type(registry)(
        employees={
            profile.identity.id: _ready(profile, frozenset({tool})),
        },
        tools=registry.tools,
        automations={},
        advisors=registry.advisors,
    )

    document = baseline_policy(ready)

    assert document.employees[profile.identity.id] == ActorGrants(
        frozenset({tool}), autonomy=profile.autonomy
    )


def _ready(profile: EmployeeProfile, tools: frozenset[str]) -> EmployeeProfile:
    sections = {
        name: SpecSection(SectionStatus.DEFINED, "defined") for name in profile.specification
    }
    return dataclasses.replace(
        profile,
        readiness=Readiness.READY,
        allowed_tools=tools,
        specification=sections,
        evaluation=EvaluationResult("suite", "1", passed=True, evaluated_at=NOW),
    )


def test_an_actor_kind_without_a_section__has_no_grants_never_a_key_error() -> None:
    """Sage on #189: a kind the document has no section for (a future one, or a user or advisor
    reaching this step) has no grants, so the engine answers UNKNOWN_ACTOR instead of raising."""
    from htco_application.access.engine import _grants_of  # noqa: PLC0415

    document = baseline_policy(read_registry(REGISTRY).registry())
    for kind in (ActorKind.USER, ActorKind.ADVISOR):
        assert _grants_of(document, ActorRef(kind, "someone")) is None
