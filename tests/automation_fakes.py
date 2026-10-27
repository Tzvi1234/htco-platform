"""Shared pieces for the durable engine's tests (slice 5.10): an in-memory store with the
Postgres store's rules, an idempotent effect book, and test definitions. The same scenarios run
against the fake (tests/application/test_durable_engine.py) and against Postgres
(tests/integration/test_durable_engine.py)."""

import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from htco_application.audit import AuditRecord
from htco_application.automation.durable import (
    Completed,
    HandlerContext,
    HandlerOutcome,
    StateChange,
    StoredExecution,
    Waiting,
)
from htco_application.jobs import JobSpec
from htco_application.kernel import JsonValue, OutboxMessage
from htco_application.status import execution_state_event
from htco_domain.automation import AutomationExecution
from tests.application.workforce_fixtures import automation_document
from tests.event_contracts import check_if_event


def engine_document(automation_id: str = "etsy-order-intake", **changes: Any) -> dict[str, Any]:
    """An active automation users with `retry_sync` may trigger, two attempts 10 s to 60 s."""
    values: dict[str, Any] = {
        "business_owner": "owner",
        "callable_from": ["event", "schedule", "user_action"],
        "retry": {"max_attempts": 2, "initial_backoff_seconds": 10, "max_backoff_seconds": 60},
        "user_capability": "retry_sync",
    } | changes
    # A None value leaves the field out (for example no user_capability).
    return automation_document(
        automation_id, **{key: value for key, value in values.items() if value is not None}
    )


@dataclass
class EffectBook:
    """A business effect keyed by receipt (an idempotent upsert); counts handler calls."""

    effects: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    failures: int = 0
    _guard: threading.Lock = field(default_factory=threading.Lock)

    contexts: list[HandlerContext] = field(default_factory=list)
    # When set, the handler's action waits for an approval instead of completing.
    wait_for_approval: bool = False
    # The action execution the wait names (the gateway's result), if any.
    waits_for: uuid.UUID | None = None

    async def handle(self, context: HandlerContext) -> HandlerOutcome:
        with self._guard:
            self.calls += 1
            self.contexts.append(context)
            if self.failures:
                self.failures -= 1
                raise ConnectionError("CRM write failed")
            if self.wait_for_approval:
                self.wait_for_approval = False
                return Waiting("pending_approval", self.waits_for)
            receipt = str(context.payload["receipt_id"])
            self.effects.setdefault(receipt, 0)
            self.effects[receipt] = 1
        return Completed({"order": receipt})


@dataclass
class MemoryExecutionStore:
    rows: dict[uuid.UUID, StoredExecution] = field(default_factory=dict)
    changes: list[StateChange] = field(default_factory=list)
    # The `automations.execution_state_changed.v1` messages, one per change (slice 5.18).
    events: list[OutboxMessage] = field(default_factory=list)
    jobs: list[JobSpec] = field(default_factory=list)
    audits: list[AuditRecord] = field(default_factory=list)
    shops: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def create(
        self,
        execution: AutomationExecution,
        payload: Mapping[str, JsonValue],
        *,
        change: StateChange,
        job: JobSpec,
    ) -> AutomationExecution:
        with self._guard:
            for stored in self.rows.values():
                found = stored.execution
                if (found.automation_id, found.idempotency_key) == (
                    execution.automation_id,
                    execution.idempotency_key,
                ):
                    return found
            self.rows[execution.id] = StoredExecution(execution, dict(payload))
            self.changes.append(change)
            self.events.append(check_if_event(execution_state_event(change)))
            self.jobs.append(job)
            return execution

    def load(self, execution_id: uuid.UUID) -> StoredExecution | None:
        return self.rows.get(execution_id)

    def advance(
        self,
        before: AutomationExecution,
        steps: Sequence[tuple[AutomationExecution, StateChange]],
        *,
        job: JobSpec | None = None,
        audit: AuditRecord | None = None,
    ) -> bool:
        with self._guard:
            stored = self.rows.get(before.id)
            if stored is None or (
                stored.execution.state,
                stored.execution.attempts,
                stored.execution.waiting_for,
            ) != (before.state, before.attempts, before.waiting_for):
                return False
            self.rows[before.id] = StoredExecution(steps[-1][0], stored.payload)
            self.changes.extend(change for _, change in steps)
            self.events.extend(check_if_event(execution_state_event(c)) for _, c in steps)
            if job is not None:
                self.jobs.append(job)
            if audit is not None:
                self.audits.append(audit)
            return True

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return self.shops.get(shop_id)

    def only(self) -> AutomationExecution:
        [stored] = self.rows.values()
        return stored.execution
