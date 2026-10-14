"""Readiness checks: is every dependency the process needs reachable right now? (FR-AUD-03)"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import anyio
import structlog

_logger = structlog.get_logger(__name__)


class ReadinessCheck(Protocol):
    """A named dependency probe that returns when the dependency is usable and raises otherwise."""

    name: str

    async def __call__(self) -> None: ...


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool


async def run_readiness_checks(
    checks: Sequence[ReadinessCheck], *, timeout_seconds: float
) -> list[CheckResult]:
    """Run every check concurrently, each within `timeout_seconds`; never raise.

    A failure is logged with the check's name and the error type only: dependency errors can
    contain hosts or credentials.
    """
    outcomes: dict[int, bool] = {}

    async def run(index: int, check: ReadinessCheck) -> None:
        try:
            with anyio.fail_after(timeout_seconds):
                await check()
        except Exception as error:  # A broken dependency must never break the probe itself.
            _logger.warning(
                "readiness.check_failed", check=check.name, error_type=type(error).__name__
            )
            outcomes[index] = False
        else:
            outcomes[index] = True

    async with anyio.create_task_group() as group:
        for index, check in enumerate(checks):
            group.start_soon(run, index, check)
    return [CheckResult(check.name, ok=outcomes[index]) for index, check in enumerate(checks)]
