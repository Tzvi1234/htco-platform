"""Readiness checks run concurrently with a timeout and never raise (FR-AUD-03)."""

import anyio
import pytest

from htco_platform.readiness import CheckResult, ReadinessCheck, run_readiness_checks


class _Check:
    def __init__(self, name: str, *, fails: bool = False, hangs: bool = False) -> None:
        self.name = name
        self.calls = 0
        self._fails = fails
        self._hangs = hangs

    async def __call__(self) -> None:
        self.calls += 1
        if self._hangs:
            await anyio.Event().wait()
        if self._fails:
            raise ConnectionError("database unreachable at 10.0.0.5")


@pytest.mark.anyio
async def test_run_readiness_checks__all_pass__reports_each_as_ok() -> None:
    checks: list[ReadinessCheck] = [_Check("database"), _Check("queue")]

    results = await run_readiness_checks(checks, timeout_seconds=1)

    assert results == [CheckResult("database", ok=True), CheckResult("queue", ok=True)]


@pytest.mark.anyio
async def test_run_readiness_checks__one_fails__reports_it_and_runs_the_others() -> None:
    queue = _Check("queue")

    results = await run_readiness_checks([_Check("database", fails=True), queue], timeout_seconds=1)

    assert results == [CheckResult("database", ok=False), CheckResult("queue", ok=True)]
    assert queue.calls == 1


@pytest.mark.anyio
async def test_run_readiness_checks__check_exceeds_timeout__reports_it_failed() -> None:
    results = await run_readiness_checks([_Check("database", hangs=True)], timeout_seconds=0.01)

    assert results == [CheckResult("database", ok=False)]


@pytest.mark.anyio
async def test_run_readiness_checks__no_checks__reports_nothing() -> None:
    assert await run_readiness_checks([], timeout_seconds=1) == []
