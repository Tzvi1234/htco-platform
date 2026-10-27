"""An in-memory task store and reader with the Postgres store's rules (slice 7.1); the
integration tests prove the same rules against real Postgres."""

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from htco_application.kernel import OutboxMessage
from htco_application.tasks import StaleTask, StoredTask, TaskDetail, TaskStep
from htco_domain.tasks import TaskKind, TaskState
from tests.event_contracts import check_if_event


@dataclass
class MemoryTasks:
    tasks: dict[uuid.UUID, StoredTask] = field(default_factory=dict)
    steps: list[TaskStep] = field(default_factory=list)
    outbox: list[OutboxMessage] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(
        self, stored: StoredTask, step: TaskStep, events: tuple[OutboxMessage, ...]
    ) -> tuple[StoredTask, bool]:
        with self._lock:
            for existing in self.tasks.values():
                if (existing.task.organization_id, existing.task.idempotency_key) == (
                    stored.task.organization_id,
                    stored.task.idempotency_key,
                ):
                    return existing, False
            self.tasks[stored.task.id] = stored
            self.steps.append(step)
            self.outbox.extend(check_if_event(e) for e in events)
            return stored, True

    def get(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> StoredTask | None:
        found = self.tasks.get(task_id)
        return (
            found if found is not None and found.task.organization_id == organization_id else None
        )

    def move(
        self,
        before: StoredTask,
        after: StoredTask,
        step: TaskStep,
        events: tuple[OutboxMessage, ...],
    ) -> StoredTask:
        with self._lock:
            current = self.tasks.get(before.task.id)
            if current is None or current.task.state is not before.task.state:
                raise StaleTask("the task moved meanwhile")
            self.tasks[after.task.id] = after
            self.steps.append(step)
            self.outbox.extend(check_if_event(e) for e in events)
            return after

    # --- TaskReader ---------------------------------------------------------------------------

    def list_tasks(  # noqa: PLR0913 - the port
        self,
        organization_id: uuid.UUID,
        *,
        limit: int,
        shop_ids: frozenset[uuid.UUID] | None = None,
        states: frozenset[TaskState] | None = None,
        kind: TaskKind | None = None,
        before: uuid.UUID | None = None,
    ) -> Sequence[StoredTask]:
        rows = sorted(
            (
                t
                for t in self.tasks.values()
                if t.task.organization_id == organization_id
                and (shop_ids is None or t.task.shop_id in shop_ids)
                and (states is None or t.task.state in states)
                and (kind is None or t.task.kind is kind)
            ),
            key=lambda t: (t.created_at, t.task.id),
            reverse=True,
        )
        if before is not None:
            marker = self.tasks[before]
            rows = [
                t for t in rows if (t.created_at, t.task.id) < (marker.created_at, marker.task.id)
            ]
        return rows[:limit]

    def task_detail(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> TaskDetail | None:
        stored = self.get(organization_id, task_id)
        if stored is None:
            return None
        return TaskDetail(stored, tuple(s for s in self.steps if s.task_id == task_id))
