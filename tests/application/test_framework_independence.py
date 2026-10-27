"""The business model needs no agent framework (Scenario J, ADR-0012).

An employee profile, a business task, an automation and an activity record are defined and
inspected through plain domain and application types; any agent runtime (LangGraph today) is
an adapter behind the `AgentRuntime` port.
"""

import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import override

import pytest

from htco_application.agents.runtime import AgentRuntime, AgentSession, SessionStatus
from htco_application.workforce.registry import Registry
from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.tasks import BusinessTask, TaskKind, TaskState
from htco_domain.workforce import EmployeeProfile
from tests.application.workforce_fixtures import (
    advisor_document,
    automation_document,
    employee_document,
    tool_document,
)


class _ScriptedRuntime(AgentRuntime):
    """A runtime with no framework at all: enough to show the port is framework-neutral."""

    name = "scripted"

    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, AgentSession] = {}

    @override
    def invoke(self, employee: EmployeeProfile, task: BusinessTask) -> AgentSession:
        session = AgentSession(
            id=uuid.uuid7(),
            employee_id=employee.identity.id,
            profile_version=employee.version,
            task_id=task.id,
            runtime=self.name,
            status=SessionStatus.INTERRUPTED,
            correlation_id=task.correlation_id,
        )
        self.sessions[session.id] = session
        return session

    @override
    def resume(self, session_id: uuid.UUID, event: str) -> AgentSession:
        return self._set(session_id, SessionStatus.COMPLETED)

    @override
    def interrupt(self, session_id: uuid.UUID, reason: str) -> AgentSession:
        return self._set(session_id, SessionStatus.INTERRUPTED)

    @override
    def cancel(self, session_id: uuid.UUID) -> AgentSession:
        return self._set(session_id, SessionStatus.CANCELLED)

    @override
    def inspect(self, session_id: uuid.UUID) -> AgentSession:
        return self.sessions[session_id]

    def _set(self, session_id: uuid.UUID, status: SessionStatus) -> AgentSession:
        self.sessions[session_id] = replace(self.sessions[session_id], status=status)
        return self.sessions[session_id]


@pytest.mark.ac("SCN-J")
def test_profile_task_automation_and_record__work_through_any_runtime() -> None:
    registry = Registry.from_documents(
        employees=[employee_document()],
        tools=[tool_document()],
        automations=[automation_document()],
        advisors=[advisor_document()],
    )
    shon = registry.employees["shon"]
    task = BusinessTask(
        id=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        shop_id=None,
        kind=TaskKind.GENERAL,
        title="Research garden sculpture suppliers",
        assignee=ActorRef(ActorKind.EMPLOYEE, shon.identity.id),
        state=TaskState.RUNNING,
        idempotency_key="research:garden-sculpture",
        correlation_id="corr-9",
    )
    runtime = _ScriptedRuntime()

    session = runtime.invoke(shon, task)
    runtime.interrupt(session.id, "waiting for supplier pages")
    finished = runtime.resume(session.id, "pages fetched")
    record = ActivityRecord(
        id=uuid.uuid7(),
        kind=ActivityKind.TASK_EXECUTION,
        organization_id=task.organization_id,
        shop_id=None,
        actor=task.assignee,
        action="research.complete",
        outcome=ActivityOutcome.SUCCEEDED,
        summary="Three candidate suppliers found.",
        occurred_at=datetime(2026, 10, 14, tzinfo=UTC),
        correlation_id=task.correlation_id,
        causation_id=str(session.id),
        task_id=task.id,
    )

    assert (finished.status, finished.employee_id, finished.profile_version) == (
        SessionStatus.COMPLETED,
        "shon",
        1,
    )
    assert runtime.cancel(session.id).status is SessionStatus.CANCELLED
    assert runtime.inspect(session.id).runtime == "scripted"
    assert record.task_id == finished.task_id


@pytest.mark.ac("SCN-J")
def test_business_model__imports_without_loading_langgraph() -> None:
    probe = (
        "import sys, htco_domain.workforce, htco_domain.tasks, htco_domain.automation, "
        "htco_domain.activity, htco_application.workforce.registry, "
        "htco_application.automation.engine, htco_application.agents.runtime, "
        "htco_application.routing.router, htco_application.decisions.advisor; "
        "print(sorted(m for m in sys.modules if m.startswith(('langgraph', 'langchain'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )

    assert result.stdout.strip() == "[]"
