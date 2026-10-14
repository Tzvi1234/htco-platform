"""The shared Decision Advisor: rules first, providers behind one port (Scenario F)."""

from decimal import Decimal

import pytest

from htco_application.decisions.advisor import (
    AdvisorUnavailable,
    DecisionAnswer,
    DecisionKind,
    DecisionOutcome,
    DecisionQuestion,
    RulesAdvisor,
    advise,
)
from htco_application.workforce.registry import Registry
from htco_application.workforce.tool_access import ToolVerdict, decide_tool_access
from htco_domain.routing import AdvisorUsage
from tests.application.workforce_fixtures import (
    advisor_document,
    automation_document,
    employee_document,
    tool_document,
)

TRIAGE = DecisionQuestion(
    id="q-1",
    kind=DecisionKind.CHOICE,
    question="Which department should handle this incoming task?",
    options=("order_assurance", "customer_service", "engineering"),
    context={"subject": "Tracking missing for receipt 3412987711", "source": "etsy"},
)


class _Provider:
    def __init__(self, name: str, answer: str | Decimal | Exception) -> None:
        self.name = name
        self._answer = answer
        self.calls = 0

    def decide(self, question: DecisionQuestion) -> DecisionAnswer:
        self.calls += 1
        if isinstance(self._answer, Exception):
            raise self._answer
        return DecisionAnswer(question.id, self._answer, provider=self.name)


def tracking_rule(question: DecisionQuestion) -> str | None:
    return (
        "order_assurance"
        if "tracking" in str(question.context.get("subject", "")).lower()
        else None
    )


RULES = RulesAdvisor((tracking_rule,))


@pytest.mark.ac("SCN-F")
def test_rules_that_decide__never_call_a_provider() -> None:
    jev = _Provider("jev", "engineering")

    answer, records = advise(TRIAGE, RULES, (jev,), AdvisorUsage.PREFERRED)

    assert answer is not None
    assert (answer.value, answer.provider) == ("order_assurance", "rules")
    assert jev.calls == 0
    assert [r.outcome for r in records] == [DecisionOutcome.ANSWERED]


@pytest.mark.ac("SCN-F")
def test_inconclusive_rules__ask_the_advisor() -> None:
    question = DecisionQuestion(
        "q-2", DecisionKind.CHOICE, "Route?", TRIAGE.options, {"subject": "Refund request"}
    )
    jev = _Provider("jev", "customer_service")

    answer, records = advise(question, RULES, (jev,), AdvisorUsage.WHEN_RULES_INCONCLUSIVE)

    assert answer is not None
    assert (answer.value, answer.provider) == ("customer_service", "jev")
    assert [(r.provider, r.outcome) for r in records] == [
        ("rules", DecisionOutcome.INCONCLUSIVE),
        ("jev", DecisionOutcome.ANSWERED),
    ]


@pytest.mark.ac("SCN-F")
def test_provider_failure__falls_back_to_the_next_provider() -> None:
    question = DecisionQuestion("q-3", DecisionKind.CHOICE, "Route?", TRIAGE.options, {})
    jev = _Provider("jev", AdvisorUnavailable("timeout"))
    fallback = _Provider("economy-model", "engineering")

    answer, records = advise(question, RULES, (jev, fallback), AdvisorUsage.WHEN_RULES_INCONCLUSIVE)

    assert answer is not None
    assert answer.provider == "economy-model"
    assert [r.outcome for r in records] == [
        DecisionOutcome.INCONCLUSIVE,
        DecisionOutcome.FAILED,
        DecisionOutcome.ANSWERED,
    ]
    assert records[1].detail == "timeout"


@pytest.mark.ac("SCN-F")
def test_answer_outside_the_schema__is_rejected_and_recorded() -> None:
    question = DecisionQuestion("q-4", DecisionKind.CHOICE, "Route?", TRIAGE.options, {})
    jev = _Provider("jev", "marketing")

    answer, records = advise(question, RULES, (jev,), AdvisorUsage.WHEN_RULES_INCONCLUSIVE)

    assert answer is None
    assert records[-1].outcome is DecisionOutcome.INVALID


@pytest.mark.parametrize(
    ("value", "valid"), [(Decimal("0.7"), True), (Decimal("1.2"), False), ("high", False)]
)
def test_score_answers__must_be_between_zero_and_one(value: str | Decimal, valid: bool) -> None:
    question = DecisionQuestion("q-5", DecisionKind.SCORE, "Risk?", (), {})

    answer, _ = advise(
        question, RulesAdvisor(()), (_Provider("jev", value),), AdvisorUsage.PREFERRED
    )

    assert (answer is not None) is valid


def test_usage_never__does_not_call_any_provider() -> None:
    question = DecisionQuestion("q-6", DecisionKind.CHOICE, "Route?", TRIAGE.options, {})
    jev = _Provider("jev", "engineering")

    answer, _ = advise(question, RULES, (jev,), AdvisorUsage.NEVER)

    assert answer is None
    assert jev.calls == 0


def test_choice_question__needs_options() -> None:
    with pytest.raises(ValueError, match="options"):
        DecisionQuestion("q", DecisionKind.CHOICE, "Route?", (), {})


@pytest.mark.ac("SCN-F")
def test_advisor_answers__never_change_authorization() -> None:
    """The advisor ranks and triages; access to tools is decided by policy alone."""
    registry = Registry.from_documents(
        employees=[employee_document(allowed_tools=["catalog.read"])],
        tools=[
            tool_document("catalog.read"),
            tool_document(
                "finance.refund", side_effect="financial", risk="critical", audit_required=True
            ),
        ],
        automations=[automation_document()],
        advisors=[advisor_document()],
    )
    question = DecisionQuestion(
        "q-7", DecisionKind.CHOICE, "Allow the refund tool?", ("allow", "deny"), {}
    )
    answer, _ = advise(
        question, RulesAdvisor(()), (_Provider("jev", "allow"),), AdvisorUsage.PREFERRED
    )

    assert answer is not None
    assert answer.value == "allow"
    assert decide_tool_access(registry, "shon", "finance.refund").verdict is ToolVerdict.DENY
