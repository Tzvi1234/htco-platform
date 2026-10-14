"""Activity records: the observable trail of everything the office does (ADR-0012).

Every meaningful operation (a task step, an automation run, a tool call, a decision, an
approval, a routing choice, a deployment) produces one record with the same identifiers, so
supervision can check completeness and the owner can ask "what did Shon do today?". Records
hold business summaries and outcomes, never private model reasoning.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from htco_domain.actors import ActorRef

MAX_SUMMARY_LENGTH = 500


class ActivityKind(StrEnum):
    TASK_EXECUTION = "task_execution"
    AUTOMATION_EXECUTION = "automation_execution"
    BUSINESS_EVENT = "business_event"
    TOOL_INVOCATION = "tool_invocation"
    MESSAGE = "message"
    DECISION = "decision"
    APPROVAL = "approval"
    INCIDENT = "incident"
    ROUTING_DECISION = "routing_decision"
    ENGINEERING_CHANGE = "engineering_change"
    RECONCILIATION = "reconciliation"
    QUALITY_FINDING = "quality_finding"


class ActivityOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    PENDING = "pending"


@dataclass(frozen=True)
class ActivityRecord:
    id: uuid.UUID
    kind: ActivityKind
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    actor: ActorRef
    action: str
    outcome: ActivityOutcome
    summary: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    task_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at needs a timezone")
        if not self.action.strip():
            raise ValueError("an activity record needs an action")
        if not self.correlation_id.strip():
            raise ValueError("an activity record needs a correlation id")
        if not self.summary.strip() or len(self.summary) > MAX_SUMMARY_LENGTH:
            raise ValueError(f"summary must be 1 to {MAX_SUMMARY_LENGTH} characters")
