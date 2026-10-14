"""The workforce model: employee identities and versioned profiles (ADR-0012)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from htco_domain.routing import AdvisorUsage, DataClass, ModelRoutingPolicy, ModelTier
from htco_domain.workforce import (
    EMPLOYEE_SPEC_SECTIONS,
    AutonomyLevel,
    Department,
    EmployeeIdentity,
    EmployeeProfile,
    EvaluationResult,
    Readiness,
    SectionStatus,
    SpecSection,
)

POLICY = ModelRoutingPolicy(
    version="maya-routing-v1",
    allowed_providers=frozenset({"anthropic"}),
    required_capabilities=frozenset(),
    default_tier=ModelTier.BALANCED,
    max_tier=ModelTier.ADVANCED,
    max_cost_per_task=Decimal("0.50"),
    max_data_class=DataClass.INTERNAL,
    fallback_tiers=(ModelTier.ECONOMY,),
    decision_advisor=AdvisorUsage.WHEN_RULES_INCONCLUSIVE,
)
MAYA = EmployeeIdentity(
    id="maya", name="Maya", title="General Manager", department=Department.EXECUTIVE_OFFICE
)
PASSED = EvaluationResult(
    suite="maya-core", version="1", passed=True, evaluated_at=datetime(2026, 10, 14, tzinfo=UTC)
)


def _sections(status: SectionStatus = SectionStatus.DRAFT) -> dict[str, SpecSection]:
    return {name: SpecSection(status, f"{name} text") for name in EMPLOYEE_SPEC_SECTIONS}


def a_profile(**changes: object) -> EmployeeProfile:
    profile = EmployeeProfile(
        identity=MAYA,
        version=1,
        autonomy=AutonomyLevel.L1,
        readiness=Readiness.DRAFT,
        allowed_tools=frozenset({"tasks.create"}),
        allowed_automations=frozenset(),
        routing=POLICY,
        specification=_sections(),
        evaluation=None,
        activation_phase=8,
    )
    return replace(profile, **changes)  # type: ignore[arg-type]


def test_departments__are_the_seven_employee_departments() -> None:
    assert len(Department) == 7


def test_spec_sections__are_the_twenty_two_of_the_specification_sheet() -> None:
    assert len(EMPLOYEE_SPEC_SECTIONS) == 22
    assert len(set(EMPLOYEE_SPEC_SECTIONS)) == 22


@pytest.mark.parametrize("bad_id", ["", "Maya", "m", "maya!", "9maya"])
def test_identity__invalid_id__is_rejected(bad_id: str) -> None:
    with pytest.raises(ValueError, match="employee id"):
        EmployeeIdentity(bad_id, "Maya", "General Manager", Department.EXECUTIVE_OFFICE)


def test_identity__blank_name_or_title__is_rejected() -> None:
    with pytest.raises(ValueError, match="name and title"):
        EmployeeIdentity("maya", " ", "General Manager", Department.EXECUTIVE_OFFICE)


def test_profile__draft__lists_every_incomplete_section() -> None:
    profile = a_profile()

    assert profile.incomplete_sections() == EMPLOYEE_SPEC_SECTIONS
    assert profile.is_production_ready() is False


def test_profile__missing_section__is_rejected() -> None:
    sections = _sections()
    del sections["evaluation"]

    with pytest.raises(ValueError, match="evaluation"):
        a_profile(specification=sections)


def test_profile__unknown_section__is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        a_profile(specification={**_sections(), "horoscope": SpecSection(SectionStatus.DRAFT, "x")})


def test_profile__version_below_one__is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        a_profile(version=0)


@pytest.mark.parametrize("readiness", [Readiness.READY, Readiness.ACTIVE])
def test_profile__production_ready_without_a_passed_evaluation__is_rejected(
    readiness: Readiness,
) -> None:
    """A configuration file existing never makes an employee production-ready."""
    with pytest.raises(ValueError, match="evaluation"):
        a_profile(readiness=readiness, specification=_sections(SectionStatus.DEFINED))


def test_profile__production_ready_with_draft_sections__is_rejected() -> None:
    with pytest.raises(ValueError, match="draft sections"):
        a_profile(readiness=Readiness.READY, evaluation=PASSED)


def test_profile__failed_evaluation__cannot_be_ready() -> None:
    failed = replace(PASSED, passed=False)

    with pytest.raises(ValueError, match="evaluation"):
        a_profile(
            readiness=Readiness.READY,
            evaluation=failed,
            specification=_sections(SectionStatus.DEFINED),
        )


def test_profile__fully_defined_and_evaluated__is_production_ready() -> None:
    profile = a_profile(
        readiness=Readiness.READY, evaluation=PASSED, specification=_sections(SectionStatus.DEFINED)
    )

    assert profile.is_production_ready() is True
    assert profile.incomplete_sections() == ()


def test_evaluation__naive_time__is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        EvaluationResult("s", "1", passed=True, evaluated_at=datetime(2026, 10, 14))  # noqa: DTZ001


def test_spec_section__empty_text__is_rejected() -> None:
    with pytest.raises(ValueError, match="text"):
        SpecSection(SectionStatus.DRAFT, "  ")
