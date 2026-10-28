"""Agent Task Scheduling & Recovery (slice 7.10): the `agent-task-scheduling-recovery` handler
with in-memory ports. A store whose claim is atomic, like `for update skip locked` with the
recovery key recorded in the same transaction. All values are invented."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.execution.gateway import UnavailableGateway
from htco_application.kernel import JobError
from htco_application.task_recovery import (
    TASK_RECOVERY,
    RecoveryNote,
    TaskRecovery,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.recovery import LapsedWork, Recovery, recovery_key

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000b001")
NOW = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
ORDER = "order:s:4000000001"


def lapsed(n: int, *, restartable: bool, effect: str | None = None) -> LapsedWork:
    return LapsedWork(
        id=uuid.UUID(int=n),
        organization_id=ORG,
        kind="task",
        lease_expired_at=NOW - timedelta(minutes=5),
        lease_generation=1,
        restartable=restartable,
        effect_key=effect,
    )


@dataclass
class MemoryRecovery:
    """The store: lapsed work, the recovery keys taken, and the outcomes applied."""

    lapsed: list[LapsedWork] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    effects: dict[str, str] = field(default_factory=dict)  # effect key -> evidence reference
    applied: list[tuple[uuid.UUID, Recovery, str | None]] = field(default_factory=list)
    notes: list[RecoveryNote] = field(default_factory=list)

    def claim_lapsed(
        self, organization_id: uuid.UUID, *, now: datetime, limit: int, claimer: str
    ) -> tuple[LapsedWork, ...]:
        del now, claimer
        claimed = []
        for work in self.lapsed:
            key = recovery_key(work)
            if work.organization_id != organization_id or key in self.taken:
                continue
            self.taken.add(key)  # the lock: one claimer per lapse
            claimed.append(work)
            if len(claimed) == limit:
                break
        return tuple(claimed)

    def recorded_effect(self, organization_id: uuid.UUID, effect_key: str) -> str | None:
        del organization_id
        return self.effects.get(effect_key)

    def apply(self, work: LapsedWork, recovery: Recovery, note: RecoveryNote) -> None:
        self.applied.append((work.id, recovery, note.evidence))
        self.notes.append(note)


@dataclass
class Resumes:
    resumed: list[uuid.UUID] = field(default_factory=list)

    async def resume(self, work: LapsedWork) -> None:
        await anyio.lowlevel.checkpoint()  # lets a second scheduler interleave
        self.resumed.append(work.id)


def handler(store: MemoryRecovery, resumes: Resumes, *, batch: int = 50) -> TaskRecovery:
    return TaskRecovery(store=store, resumer=resumes, clock=lambda: NOW, batch=batch)


def context(payload: dict[str, Any] | None = None) -> HandlerContext:
    return HandlerContext(
        execution_id=uuid.uuid7(),
        automation_id=TASK_RECOVERY,
        actor=ActorRef(ActorKind.SYSTEM, "scheduler"),
        organization_id=ORG,
        shop_id=None,
        correlation_id="sched:recovery",
        causation_id=None,
        attempt=1,
        payload=payload if payload is not None else {"mode": "scan"},
        gateway=UnavailableGateway(),
    )


@pytest.mark.ac("AC-7.10.1")
async def test_ac_7_10_1_restartable_resumes_once_the_rest_escalates_both_recorded() -> None:
    store = MemoryRecovery([lapsed(1, restartable=True), lapsed(2, restartable=False)])
    resumes = Resumes()

    first = await handler(store, resumes)(context())
    again = await handler(store, resumes)(context())

    assert resumes.resumed == [uuid.UUID(int=1)]
    assert store.applied == [
        (uuid.UUID(int=1), Recovery.RESUME, None),
        (uuid.UUID(int=2), Recovery.ESCALATE, None),
    ]
    assert {n.recovery for n in store.notes} == {Recovery.RESUME, Recovery.ESCALATE}
    assert all(n.key == recovery_key(w) for n, w in zip(store.notes, store.lapsed, strict=True))
    assert first == Completed({"claimed": 2, "resumed": 1, "escalated": 1, "closed": 0})
    assert again == Completed({"claimed": 0, "resumed": 0, "escalated": 0, "closed": 0})


@pytest.mark.ac("AC-7.10.2")
async def test_ac_7_10_2_a_recorded_effect_closes_with_its_evidence_and_runs_nothing() -> None:
    store = MemoryRecovery(
        [lapsed(3, restartable=True, effect=ORDER)], effects={ORDER: "etsy_orders:o-9"}
    )
    resumes = Resumes()

    outcome = await handler(store, resumes)(context())

    assert resumes.resumed == []
    assert store.applied == [(uuid.UUID(int=3), Recovery.CLOSE, "etsy_orders:o-9")]
    assert outcome == Completed({"claimed": 1, "resumed": 0, "escalated": 0, "closed": 1})


@pytest.mark.ac("AC-7.10.3")
async def test_ac_7_10_3_two_schedulers_never_recover_the_same_task_twice() -> None:
    store = MemoryRecovery([lapsed(n, restartable=True) for n in range(10, 30)])
    resumes = Resumes()

    async with anyio.create_task_group() as group:
        for _ in range(2):
            group.start_soon(handler(store, resumes, batch=3), context())
            group.start_soon(handler(store, resumes, batch=3), context())

    remaining = 20 - len(resumes.resumed)
    while remaining:  # later scans take the rest
        await handler(store, resumes, batch=3)(context())
        remaining = 20 - len(resumes.resumed)
    assert sorted(resumes.resumed) == [uuid.UUID(int=n) for n in range(10, 30)]
    assert len(store.applied) == 20


async def test_a_failed_resume_escalates_rather_than_losing_the_task() -> None:
    @dataclass
    class Broken:
        async def resume(self, work: LapsedWork) -> None:
            del work
            raise ConnectionError("the queue went away")

    store = MemoryRecovery([lapsed(4, restartable=True)])

    outcome = await TaskRecovery(store=store, resumer=Broken(), clock=lambda: NOW)(context())

    assert store.applied == [(uuid.UUID(int=4), Recovery.ESCALATE, None)]
    assert store.notes[0].reason == "resume_failed"
    assert outcome == Completed({"claimed": 1, "resumed": 0, "escalated": 1, "closed": 0})


async def test_a_run_names_the_scan_mode() -> None:
    with pytest.raises(JobError):
        await handler(MemoryRecovery(), Resumes())(context({}))


def test_the_batch_is_bounded() -> None:
    with pytest.raises(ValueError, match="batch"):
        handler(MemoryRecovery(), Resumes(), batch=0)
