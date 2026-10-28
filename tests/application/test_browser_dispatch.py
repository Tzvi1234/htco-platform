"""The browser worker's dispatch ports (slice 8A.2c): the lease heartbeat (AC-8A.2.21) and the
shapes that keep tokens out of logs and page text out of outcomes."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.browser_dispatch import (
    BrowserJobLease,
    LeaseLostError,
    RunBinding,
    RunOutcome,
    StartedRun,
    lease_heartbeat,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def lease() -> BrowserJobLease:
    return BrowserJobLease(
        job_id=uuid.uuid4(), kind="browser.run", organization_id=uuid.uuid4(),
        shop_id=uuid.uuid4(), payload={}, attempts=1, lease_expires_at=NOW,
        token="secret-lease-token",  # noqa: S106 - a fake
    )  # fmt: skip


class Queue:
    def __init__(self, *, lose_after: int | None = None, broken: bool = False) -> None:
        self.renewals = 0
        self.lose_after = lose_after
        self.broken = broken

    async def claim(self, *, worker_id: str, lease: timedelta) -> BrowserJobLease | None:  # noqa: ARG002  # pragma: no cover
        return None

    async def renew(self, job: BrowserJobLease, *, lease: timedelta) -> datetime:  # noqa: ARG002
        if self.broken:
            raise RuntimeError("database gone")
        if self.lose_after is not None and self.renewals >= self.lose_after:
            raise LeaseLostError("gone")
        self.renewals += 1
        return NOW

    async def complete(self, job: BrowserJobLease) -> None:  # noqa: ARG002  # pragma: no cover
        return None

    async def fail(self, job: BrowserJobLease, *, error: str, retry_at: datetime | None) -> None:  # noqa: ARG002  # pragma: no cover
        return None


@pytest.mark.ac("AC-8A.2.21")
async def test_heartbeat__renews_while_the_run_goes_on_and_stops_after() -> None:
    queue = Queue()
    async with lease_heartbeat(
        queue, lease(), every=timedelta(seconds=0.02), lease=timedelta(seconds=1)
    ) as beat:
        await asyncio.sleep(0.15)
    renewed = queue.renewals
    await asyncio.sleep(0.06)

    assert renewed >= 3
    assert beat.beats == renewed
    assert queue.renewals == renewed  # no renewal after the block
    assert not beat.lost


@pytest.mark.ac("AC-8A.2.21")
@pytest.mark.parametrize("queue", [Queue(lose_after=1), Queue(broken=True)])
async def test_heartbeat__a_lost_or_failed_renewal_stops_the_beat(
    queue: Queue, caplog: pytest.LogCaptureFixture
) -> None:
    async with lease_heartbeat(
        queue, lease(), every=timedelta(seconds=0.02), lease=timedelta(seconds=1)
    ) as beat:
        await asyncio.sleep(0.12)

    assert beat.lost
    assert "secret" not in caplog.text
    assert "database gone" not in caplog.text


def test_heartbeat__must_beat_more_often_than_the_lease_runs_out() -> None:
    with pytest.raises(ValueError, match="more often"):
        asyncio.run(
            lease_heartbeat(
                Queue(), lease(), every=timedelta(seconds=5), lease=timedelta(seconds=5)
            ).__aenter__()
        )


def test_tokens__never_in_a_repr() -> None:
    run = StartedRun(
        binding=RunBinding(
            action_execution_id=uuid.uuid4(), attempt=1, environment_id=uuid.uuid4(),
            action_name="autova.order.read", recipe_id="autova.order_status", recipe_version=1,
            scope_id=None, parameters={}, request_hash="a" * 64, is_write=False, deadline=NOW,
        ),
        run_token="secret-run-token",  # noqa: S106 - a fake
    )  # fmt: skip

    assert "secret" not in repr(run)
    assert "secret" not in repr(lease())


@pytest.mark.parametrize("status", ["interrupted", "started", "done"])
def test_outcome__only_the_worker_s_final_statuses(status: str) -> None:
    with pytest.raises(ValueError, match="ends"):
        RunOutcome(status=status)


def test_outcome__an_error_is_a_code_never_page_text() -> None:
    with pytest.raises(ValueError, match="code"):
        RunOutcome(status="failed", error_code="Page said: wrong password for x@y")
    assert RunOutcome(status="challenge", error_code="captcha").error_code == "captcha"
