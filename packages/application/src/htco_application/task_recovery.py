"""Agent Task Scheduling & Recovery (slice 7.10, FR-TASK-04): the
`agent-task-scheduling-recovery` handler. Deterministic: no model, no Etsy call.

On the schedule (`mode = "scan"`, per organization) it claims a bounded batch of lapsed work
(tasks and automation executions whose lease expired). The store's claim is the lock: each lapse
(its lease generation) is claimed by one scheduler only, so two instances never recover the same
work twice (AC-7.10.3). For each claimed item:

- its business effect is already recorded (`effect_key`): closed with that evidence, nothing runs
  again (AC-7.10.2);
- it is marked restartable: resumed from its checkpoint, once (AC-7.10.1);
- otherwise, or when the resume fails: escalated to a person (AC-7.10.1).

Every outcome is applied with a `RecoveryNote` keyed by the lapse, which the store writes as the
activity record (slice 7.7) in the same transaction as the work's new state.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.kernel import JobError, Offload, run_inline
from htco_domain.recovery import LapsedWork, Recovery, decide_recovery, recovery_key

TASK_RECOVERY = "agent-task-scheduling-recovery"
RECOVERY_BATCH = 50

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryNote:
    """What the activity record says: identifiers and codes only."""

    key: str  # recovery:{kind}:{id}:{generation}
    recovery: Recovery
    reason: str  # "effect_recorded", "restartable", "not_restartable", "resume_failed"
    evidence: str | None  # the recorded effect's reference, for a close
    execution_id: uuid.UUID
    correlation_id: str


class RecoveryStore(Protocol):
    """Synchronous (psycopg's pool); run through `offload`."""

    def claim_lapsed(
        self, organization_id: uuid.UUID, *, now: datetime, limit: int, claimer: str
    ) -> tuple[LapsedWork, ...]:
        """Lapsed work not yet claimed for its lease generation, taken atomically (skip locked)."""
        ...

    def recorded_effect(self, organization_id: uuid.UUID, effect_key: str) -> str | None:
        """The reference of the recorded business effect, if any (for example an order's row)."""
        ...

    def apply(self, work: LapsedWork, recovery: Recovery, note: RecoveryNote) -> None:
        """The work's new state and the activity record, in one transaction."""
        ...


class Resumer(Protocol):
    async def resume(self, work: LapsedWork) -> None:
        """Re-enqueue the work from its checkpoint (idempotent by its recovery key)."""
        ...


@dataclass
class TaskRecovery:
    """The `agent-task-scheduling-recovery` handler."""

    store: RecoveryStore
    resumer: Resumer
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    batch: int = RECOVERY_BATCH

    def __post_init__(self) -> None:
        if self.batch < 1:
            raise ValueError("a recovery batch takes at least one item")

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        if ctx.payload.get("mode") != "scan":
            raise JobError("a recovery run names the scan mode")
        now = self.clock()
        claimed = await self.offload(
            lambda: self.store.claim_lapsed(
                ctx.organization_id, now=now, limit=self.batch, claimer=str(ctx.execution_id)
            )
        )
        counts = {Recovery.RESUME: 0, Recovery.ESCALATE: 0, Recovery.CLOSE: 0}
        for work in claimed:
            counts[await self._recover(ctx, work)] += 1
        return Completed(
            {
                "claimed": len(claimed),
                "resumed": counts[Recovery.RESUME],
                "escalated": counts[Recovery.ESCALATE],
                "closed": counts[Recovery.CLOSE],
            }
        )

    async def _recover(self, ctx: HandlerContext, work: LapsedWork) -> Recovery:
        effect_key = work.effect_key
        evidence = (
            None
            if effect_key is None
            else await self.offload(
                lambda: self.store.recorded_effect(work.organization_id, effect_key)
            )
        )
        recovery = decide_recovery(work, effect_recorded=evidence is not None)
        reason = {
            Recovery.CLOSE: "effect_recorded",
            Recovery.RESUME: "restartable",
            Recovery.ESCALATE: "not_restartable",
        }[recovery]
        if recovery is Recovery.RESUME:
            try:
                await self.resumer.resume(work)
            except Exception:
                _log.warning("recovery.resume_failed: %s %s", work.kind, work.id)
                recovery, reason = Recovery.ESCALATE, "resume_failed"
        note = RecoveryNote(
            key=recovery_key(work),
            recovery=recovery,
            reason=reason,
            evidence=evidence,
            execution_id=ctx.execution_id,
            correlation_id=ctx.correlation_id,
        )
        await self.offload(lambda: self.store.apply(work, recovery, note))
        return recovery
