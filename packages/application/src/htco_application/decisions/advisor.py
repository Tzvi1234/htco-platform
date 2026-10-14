"""The shared Decision Advisor: provider-independent structured decisions (ADR-0015).

Deterministic rules decide first; only an inconclusive question reaches a provider (Jev, an
economy model, or any later decision service), tried in order with schema validation and a
record of every attempt. An advisor answers triage and ranking questions: it never authorizes
anything, so security, payment and permission policies stay with the policy engine.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from htco_domain.routing import AdvisorUsage

RULES_PROVIDER = "rules"


class DecisionKind(StrEnum):
    CHOICE = "choice"  # pick one of `options`
    SCORE = "score"  # a number between 0 and 1


@dataclass(frozen=True)
class DecisionQuestion:
    id: str
    kind: DecisionKind
    question: str
    options: tuple[str, ...]
    context: Mapping[str, str | int | Decimal]

    def __post_init__(self) -> None:
        if self.kind is DecisionKind.CHOICE and not self.options:
            raise ValueError("a choice question needs options")


@dataclass(frozen=True)
class DecisionAnswer:
    question_id: str
    value: str | Decimal
    provider: str


class DecisionOutcome(StrEnum):
    ANSWERED = "answered"
    INCONCLUSIVE = "inconclusive"  # rules had no answer
    INVALID = "invalid"  # the provider answered outside the question's schema
    FAILED = "failed"  # the provider was unavailable or errored


@dataclass(frozen=True)
class DecisionRecord:
    """One attempt, for the decision log and the advisor benchmarks."""

    question_id: str
    provider: str
    outcome: DecisionOutcome
    value: str | Decimal | None
    detail: str


class AdvisorUnavailable(Exception):  # noqa: N818 - reads as "advisor (is) unavailable"
    pass


class DecisionAdvisor(Protocol):
    name: str

    def decide(self, question: DecisionQuestion) -> DecisionAnswer: ...


type Rule = Callable[[DecisionQuestion], str | None]


@dataclass(frozen=True)
class RulesAdvisor:
    """Ordered deterministic rules; the first that answers wins."""

    rules: Sequence[Rule]

    def decide(self, question: DecisionQuestion) -> str | None:
        for rule in self.rules:
            answer = rule(question)
            if answer is not None:
                return answer
        return None


def _valid(question: DecisionQuestion, value: str | Decimal) -> bool:
    if question.kind is DecisionKind.CHOICE:
        return isinstance(value, str) and value in question.options
    return isinstance(value, Decimal) and Decimal(0) <= value <= Decimal(1)


def advise(
    question: DecisionQuestion,
    rules: RulesAdvisor,
    providers: Sequence[DecisionAdvisor],
    usage: AdvisorUsage,
) -> tuple[DecisionAnswer | None, list[DecisionRecord]]:
    """Answer `question` by rules, else by the first provider with a valid answer.

    Returns no answer when nothing valid came back; the caller then escalates (a more capable
    model or a person). Every attempt is in the returned records.
    """
    records: list[DecisionRecord] = []
    ruled = rules.decide(question)
    if ruled is not None and _valid(question, ruled):
        records.append(
            DecisionRecord(question.id, RULES_PROVIDER, DecisionOutcome.ANSWERED, ruled, "")
        )
        return DecisionAnswer(question.id, ruled, RULES_PROVIDER), records
    records.append(
        DecisionRecord(question.id, RULES_PROVIDER, DecisionOutcome.INCONCLUSIVE, None, "")
    )
    if usage is AdvisorUsage.NEVER:
        return None, records

    for provider in providers:
        try:
            answer = provider.decide(question)
        except Exception as error:
            records.append(
                DecisionRecord(question.id, provider.name, DecisionOutcome.FAILED, None, str(error))
            )
            continue
        if not _valid(question, answer.value):
            records.append(
                DecisionRecord(
                    question.id,
                    provider.name,
                    DecisionOutcome.INVALID,
                    answer.value,
                    "answer outside the question's schema",
                )
            )
            continue
        records.append(
            DecisionRecord(question.id, provider.name, DecisionOutcome.ANSWERED, answer.value, "")
        )
        return answer, records
    return None, records
