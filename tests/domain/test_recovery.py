"""Recovering lapsed work (slice 7.10): what happens to a task or execution whose lease expired."""

import uuid
from datetime import UTC, datetime

import pytest

from htco_domain.recovery import LapsedWork, Recovery, decide_recovery, recovery_key

LAPSED_AT = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)


def work(*, restartable: bool, generation: int = 1) -> LapsedWork:
    return LapsedWork(
        id=uuid.UUID(int=7),
        organization_id=uuid.UUID(int=1),
        kind="task",
        lease_expired_at=LAPSED_AT,
        lease_generation=generation,
        restartable=restartable,
        effect_key="order:s:4000000001",
    )


@pytest.mark.ac("AC-7.10.2")
def test_ac_7_10_2_recorded_effect_closes_whatever_else_is_true() -> None:
    assert decide_recovery(work(restartable=True), effect_recorded=True) is Recovery.CLOSE
    assert decide_recovery(work(restartable=False), effect_recorded=True) is Recovery.CLOSE


@pytest.mark.ac("AC-7.10.1")
def test_ac_7_10_1_restartable_resumes_the_rest_escalates() -> None:
    assert decide_recovery(work(restartable=True), effect_recorded=False) is Recovery.RESUME
    assert decide_recovery(work(restartable=False), effect_recorded=False) is Recovery.ESCALATE


def test_each_lapse_has_its_own_key() -> None:
    assert recovery_key(work(restartable=True)) == f"recovery:task:{uuid.UUID(int=7)}:1"
    assert recovery_key(work(restartable=True)) != recovery_key(
        work(restartable=True, generation=2)
    )


def test_lapsed_work_is_well_formed() -> None:
    with pytest.raises(ValueError, match="timezone"):
        LapsedWork(uuid.UUID(int=7), uuid.UUID(int=1), "task",
                   datetime(2026, 10, 27), 1, restartable=True)  # noqa: DTZ001  # fmt: skip
    with pytest.raises(ValueError, match="kind"):
        LapsedWork(uuid.UUID(int=7), uuid.UUID(int=1), "lunch", LAPSED_AT, 1, restartable=True)
    with pytest.raises(ValueError, match="generation"):
        LapsedWork(uuid.UUID(int=7), uuid.UUID(int=1), "task", LAPSED_AT, 0, restartable=True)
