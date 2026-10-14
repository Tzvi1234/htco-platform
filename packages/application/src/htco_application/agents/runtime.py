"""The agent runtime port: how the office runs an employee's reasoning (ADR-0003, ADR-0012).

LangGraph is today's adapter (`htco_agent_runtime`); the Claude Agent SDK, Claude Managed
Agents or a hybrid could implement the same port. Sessions reference employees, profile
versions and business tasks by ID; runtime state (graphs, checkpoints) never becomes the
source of truth for business records.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from htco_domain.tasks import BusinessTask
from htco_domain.workforce import EmployeeProfile


class SessionStatus(StrEnum):
    RUNNING = "running"
    INTERRUPTED = "interrupted"  # waiting for an event, an approval or a person
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AgentSession:
    id: uuid.UUID
    employee_id: str
    profile_version: int
    task_id: uuid.UUID
    runtime: str
    status: SessionStatus
    correlation_id: str


class AgentRuntime(Protocol):
    name: str

    def invoke(self, employee: EmployeeProfile, task: BusinessTask) -> AgentSession: ...

    def resume(self, session_id: uuid.UUID, event: str) -> AgentSession: ...

    def interrupt(self, session_id: uuid.UUID, reason: str) -> AgentSession: ...

    def cancel(self, session_id: uuid.UUID) -> AgentSession: ...

    def inspect(self, session_id: uuid.UUID) -> AgentSession: ...
