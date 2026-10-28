"""Policy engine v1 values (slice 7.2): a versioned policy document and its decisions.

The document is reviewed data: every grant names a capability of its catalogue, only employees
carry an autonomy level, and only an L3 employee (or an automation or system identity) may hold
a pre-approval. Advisors have no section at all, so a policy cannot grant them anything.
"""

import uuid
from datetime import UTC, datetime

import pytest

from htco_domain.actors import ActorKind, ActorRef
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
from htco_domain.workforce import AutonomyLevel

ORG = uuid.UUID("00000000-0000-7000-8000-0000000000a0")
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
READ = CapabilitySpec(SideEffect.NONE, RiskLevel.LOW)
WRITE = CapabilitySpec(SideEffect.EXTERNAL, RiskLevel.HIGH)


def a_document(**overrides: object) -> PolicyDocument:
    fields: dict[str, object] = {
        "capabilities": {"orders.read": READ, "etsy.tracking.upload": WRITE},
        "employees": {
            "maya": ActorGrants(frozenset({"orders.read"}), autonomy=AutonomyLevel.L1),
        },
        "automations": {"tracking-sync": ActorGrants(frozenset({"orders.read"}))},
        "systems": {"outbox": ActorGrants(frozenset({"etsy.tracking.upload"}))},
    }
    fields.update(overrides)
    return PolicyDocument(**fields)  # type: ignore[arg-type]


# --- reasons and verdicts -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "verdict"),
    [
        (Reason.GRANTED, Verdict.ALLOW),
        (Reason.PRE_APPROVED, Verdict.ALLOW),
        (Reason.ROLE_REQUIRES_APPROVAL, Verdict.REQUIRE_APPROVAL),
        (Reason.ACTS_OUTSIDE_HTCO, Verdict.REQUIRE_APPROVAL),
        (Reason.CRITICAL_RISK, Verdict.REQUIRE_APPROVAL),
        (Reason.NO_POLICY, Verdict.DENY),
        (Reason.UNKNOWN_CAPABILITY, Verdict.DENY),
        (Reason.UNKNOWN_ACTOR, Verdict.DENY),
        (Reason.ADVISOR_HAS_NO_CAPABILITIES, Verdict.DENY),
        (Reason.NOT_GRANTED, Verdict.DENY),
        (Reason.OTHER_ORGANIZATION, Verdict.DENY),
        (Reason.SHOP_NOT_GRANTED, Verdict.DENY),
        (Reason.NO_ORIGINATING_USER, Verdict.DENY),
        (Reason.AUTONOMY_TOO_LOW, Verdict.DENY),
        (Reason.MFA_REQUIRED, Verdict.DENY),
        (Reason.STEP_UP_REQUIRED, Verdict.DENY),
    ],
)
def test_reason__fixes_the_verdict(reason: Reason, verdict: Verdict) -> None:
    assert reason.verdict is verdict


def test_reason__every_reason_has_a_verdict() -> None:
    assert {reason.verdict for reason in Reason} == set(Verdict)


def test_decision__reads_its_verdict_from_the_reason() -> None:
    decision = PolicyDecision(
        Reason.ACTS_OUTSIDE_HTCO, "etsy.tracking.upload", ActorRef(ActorKind.SYSTEM, "outbox"), 3
    )

    assert decision.verdict is Verdict.REQUIRE_APPROVAL
    assert decision.policy_version == 3


def test_decision__a_version_starts_at_one() -> None:
    with pytest.raises(ValueError, match="version"):
        PolicyDecision(Reason.GRANTED, "orders.read", ActorRef(ActorKind.SYSTEM, "api"), 0)


@pytest.mark.parametrize("reason", [r for r in Reason if r is not Reason.NO_POLICY])
def test_decision__only_a_missing_policy_has_no_version(reason: Reason) -> None:
    with pytest.raises(ValueError, match="version"):
        PolicyDecision(reason, "orders.read", ActorRef(ActorKind.SYSTEM, "api"), None)


def test_decision__a_missing_policy_names_no_version() -> None:
    with pytest.raises(ValueError, match="version"):
        PolicyDecision(Reason.NO_POLICY, "orders.read", ActorRef(ActorKind.SYSTEM, "api"), 2)


# --- capabilities -----------------------------------------------------------------------


@pytest.mark.parametrize("side_effect", [SideEffect.EXTERNAL, SideEffect.FINANCIAL])
def test_capability__outside_htco_is_at_least_medium_risk(side_effect: SideEffect) -> None:
    with pytest.raises(ValueError, match="medium"):
        CapabilitySpec(side_effect, RiskLevel.LOW)


# --- the document -------------------------------------------------------------------------


def test_document__a_valid_document_is_accepted() -> None:
    document = a_document()

    assert document.capabilities["orders.read"] == READ


@pytest.mark.parametrize("name", ["", "Orders", "orders..read", "orders.read!", "x" * 101, ".a"])
def test_document__a_capability_name_is_lowercase_dotted(name: str) -> None:
    with pytest.raises(ValueError, match="capability name"):
        a_document(capabilities={name: READ})


@pytest.mark.parametrize("section", ["employees", "automations", "systems"])
def test_document__a_grant_names_a_capability_of_the_catalogue(section: str) -> None:
    grants = ActorGrants(
        frozenset({"orders.delete"}),
        autonomy=AutonomyLevel.L1 if section == "employees" else None,
    )

    with pytest.raises(ValueError, match=r"unknown capability 'orders\.delete'"):
        a_document(**{section: {"someone": grants}})


@pytest.mark.parametrize("actor_id", ["", " ", "x" * 201])
def test_document__an_actor_id_is_short_and_not_blank(actor_id: str) -> None:
    with pytest.raises(ValueError, match="actor id"):
        a_document(automations={actor_id: ActorGrants(frozenset({"orders.read"}))})


def test_document__an_employee_needs_an_autonomy_level() -> None:
    with pytest.raises(ValueError, match="autonomy"):
        a_document(employees={"maya": ActorGrants(frozenset({"orders.read"}))})


@pytest.mark.parametrize("section", ["automations", "systems"])
def test_document__only_employees_have_an_autonomy_level(section: str) -> None:
    grants = ActorGrants(frozenset({"orders.read"}), autonomy=AutonomyLevel.L3)

    with pytest.raises(ValueError, match="autonomy"):
        a_document(**{section: {"someone": grants}})


def test_grants__a_pre_approval_is_one_of_the_grants() -> None:
    with pytest.raises(ValueError, match="pre-approved"):
        ActorGrants(frozenset({"orders.read"}), pre_approved=frozenset({"etsy.tracking.upload"}))


@pytest.mark.parametrize("level", [AutonomyLevel.L0, AutonomyLevel.L1, AutonomyLevel.L2])
def test_document__an_employee_below_l3_holds_no_pre_approval(level: AutonomyLevel) -> None:
    grants = ActorGrants(
        frozenset({"etsy.tracking.upload"}),
        pre_approved=frozenset({"etsy.tracking.upload"}),
        autonomy=level,
    )

    with pytest.raises(ValueError, match="L3"):
        a_document(employees={"dana": grants})


def test_document__an_l3_employee_may_hold_a_pre_approval() -> None:
    grants = ActorGrants(
        frozenset({"etsy.tracking.upload"}),
        pre_approved=frozenset({"etsy.tracking.upload"}),
        autonomy=AutonomyLevel.L3,
    )

    assert a_document(employees={"dana": grants}).employees["dana"] == grants


def test_document__is_read_only() -> None:
    document = a_document()

    with pytest.raises(TypeError):
        document.capabilities["orders.read"] = WRITE  # type: ignore[index]


def test_document__round_trips_through_json() -> None:
    document = a_document(
        employees={
            "dana": ActorGrants(
                frozenset({"etsy.tracking.upload", "orders.read"}),
                pre_approved=frozenset({"etsy.tracking.upload"}),
                autonomy=AutonomyLevel.L3,
            )
        }
    )

    assert PolicyDocument.from_json(document.to_json()) == document


def test_document__json_is_stable_and_sorted() -> None:
    data = a_document().to_json()

    assert data == {
        "capabilities": {
            "etsy.tracking.upload": {"side_effect": "external", "risk": "high"},
            "orders.read": {"side_effect": "none", "risk": "low"},
        },
        "employees": {
            "maya": {"capabilities": ["orders.read"], "pre_approved": [], "autonomy": "L1"}
        },
        "automations": {
            "tracking-sync": {"capabilities": ["orders.read"], "pre_approved": [], "autonomy": None}
        },
        "systems": {
            "outbox": {
                "capabilities": ["etsy.tracking.upload"],
                "pre_approved": [],
                "autonomy": None,
            }
        },
    }


@pytest.mark.parametrize(
    "data",
    [
        {"capabilities": {}, "employees": {}, "automations": {}},
        {"capabilities": {}, "employees": {}, "automations": {}, "systems": {}, "advisors": {}},
        {
            "capabilities": {"a.b": {"side_effect": "none", "risk": "low", "extra": 1}},
            "employees": {},
            "automations": {},
            "systems": {},
        },
        {
            "capabilities": {"a.b": {"side_effect": "sometimes", "risk": "low"}},
            "employees": {},
            "automations": {},
            "systems": {},
        },
        {
            "capabilities": {},
            "employees": {},
            "automations": {"x": {"capabilities": "a.b", "pre_approved": [], "autonomy": None}},
            "systems": {},
        },
        {
            "capabilities": {},
            "employees": {"x": {"capabilities": [], "pre_approved": [], "autonomy": "L9"}},
            "automations": {},
            "systems": {},
        },
        [],
    ],
)
def test_document__malformed_json_is_rejected(data: object) -> None:
    with pytest.raises(ValueError, match="policy document"):
        PolicyDocument.from_json(data)


# --- versions -----------------------------------------------------------------------------


def test_version__starts_at_one() -> None:
    with pytest.raises(ValueError, match="version"):
        PolicyVersion(ORG, 0, a_document(), ActorRef(ActorKind.SYSTEM, "api"), NOW)


def test_version__needs_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        PolicyVersion(
            ORG,
            1,
            a_document(),
            ActorRef(ActorKind.SYSTEM, "api"),
            datetime(2026, 10, 27, 9, 0),  # noqa: DTZ001 - the point of the test
        )


@pytest.mark.parametrize(
    "reach", [{"shops": None}, {"shops": frozenset({uuid.UUID(int=1)})}, {"masked": True}]
)
def test_a_denial_reaches_no_shop_and_masks_nothing(reach: dict[str, object]) -> None:
    """Quill2 on #189: what a decision reaches is part of it, and a denial reaches nothing."""
    with pytest.raises(ValueError, match="reaches nothing"):
        PolicyDecision(
            Reason.NOT_GRANTED,
            "orders.read",
            ActorRef(ActorKind.SYSTEM, "api"),
            1,
            **reach,  # type: ignore[arg-type]
        )
