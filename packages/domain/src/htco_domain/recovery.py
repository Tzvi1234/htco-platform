"""Recovering lapsed work (slice 7.10, FR-TASK-04). No I/O.

A task or an automation execution whose lease expired is recovered once per lapse (its lease
generation): closed with the evidence when its business effect is already recorded (the order
exists, AC-7.10.2), resumed when it is marked safely restartable, and escalated to a person
otherwise (AC-7.10.1). A completed business action is never repeated.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

WORK_KINDS = frozenset({"task", "execution"})


class Recovery(StrEnum):
    CLOSE = "close"  # the effect is recorded: close with that evidence, run nothing
    RESUME = "resume"  # restartable: resume from its checkpoint
    ESCALATE = "escalate"  # not restartable: a person decides


@dataclass(frozen=True)
class LapsedWork:
    id: uuid.UUID
    organization_id: uuid.UUID
    kind: str  # "task" or "execution"
    lease_expired_at: datetime
    lease_generation: int  # bumped by every claim, so each lapse is recovered once
    restartable: bool
    # What its business effect would be recorded under (for example `order:{shop}:{receipt}`).
    effect_key: str | None = None

    def __post_init__(self) -> None:
        if self.lease_expired_at.tzinfo is None:
            raise ValueError("lease_expired_at needs a timezone")
        if self.kind not in WORK_KINDS:
            raise ValueError(f"work kind is one of {sorted(WORK_KINDS)}")
        if self.lease_generation < 1:
            raise ValueError("a lease generation starts at 1")


def decide_recovery(work: LapsedWork, *, effect_recorded: bool) -> Recovery:
    """The recorded effect first: a restartable task whose effect exists is still closed."""
    if effect_recorded:
        return Recovery.CLOSE
    return Recovery.RESUME if work.restartable else Recovery.ESCALATE


def recovery_key(work: LapsedWork) -> str:
    return f"recovery:{work.kind}:{work.id}:{work.lease_generation}"
