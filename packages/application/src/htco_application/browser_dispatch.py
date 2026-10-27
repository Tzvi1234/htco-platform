"""The browser worker's side of browser dispatch (slice 8A.2c, ADR-0025 §3, AC-8A.2.18 to 2.28).

The agent-worker's gateway (#143) moves a Browser binding's action to `DISPATCHED`, stores the
routed binding with the claim and enqueues one `browser.run` job, a locator only. This module is
what the browser worker does with it, through three async ports (AUD-05):

- `BrowserJobQueue`: it claims `browser.*` jobs only, each with an unguessable lease token, and
  renews the lease on a heartbeat (`lease_heartbeat`) while the run goes on.
- `BrowserRuns`: it starts the attempt with the leased job, on the profile lock's own connection
  (`start`), and gets the stored binding and a run token only if its start created the `started`
  row; a redelivered job gets nothing. It records the outcome with the run token (`finish`), which
  works after the job's lease expired, once.
- The profile archives are reached through the leased job too (the archive adapter).

The database decides every check again in the same transaction as the started marker; nothing
here is trusted for authority. Browser runs stay off in production until the #95 blockers hold.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.kernel import JsonValue

RUN_JOB = "browser.run"
FINAL_STATUSES = frozenset({"succeeded", "failed", "challenge", "refused", "timeout"})
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserJobLease:
    """A claimed `browser.*` job. The token is the caller's proof of the lease; never logged."""

    job_id: uuid.UUID
    kind: str
    organization_id: uuid.UUID | None
    shop_id: uuid.UUID | None
    payload: Mapping[str, JsonValue]
    attempts: int
    lease_expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class RunBinding:
    """What the run is allowed to execute, loaded from the stored claim, never from the job."""

    action_execution_id: uuid.UUID
    attempt: int
    environment_id: uuid.UUID
    action_name: str
    recipe_id: str
    recipe_version: int
    scope_id: str | None
    parameters: Mapping[str, JsonValue]
    request_hash: str
    is_write: bool
    deadline: datetime


@dataclass(frozen=True)
class StartedRun:
    binding: RunBinding
    run_token: str = field(repr=False)


@dataclass(frozen=True)
class RefusedRun:
    """The start refused the run with its code; a final row and its event were written."""

    action_execution_id: uuid.UUID
    attempt: int
    code: str


@dataclass(frozen=True)
class RunOutcome:
    status: str
    error_code: str | None = None
    output: Mapping[str, JsonValue] | None = None
    evidence: Sequence[JsonValue] = ()

    def __post_init__(self) -> None:
        if self.status not in FINAL_STATUSES:
            raise ValueError("a run ends succeeded, failed, challenge, refused or timeout")
        if self.error_code is not None and not _CODE.match(self.error_code):
            raise ValueError("an error is a short code, never page text")


class BrowserJobQueue(Protocol):
    async def claim(self, *, worker_id: str, lease: timedelta) -> BrowserJobLease | None: ...

    async def renew(self, job: BrowserJobLease, *, lease: timedelta) -> datetime:
        """The new expiry; raises `LeaseLostError` when the lease is gone."""
        ...

    async def complete(self, job: BrowserJobLease) -> None: ...

    async def fail(
        self, job: BrowserJobLease, *, error: str, retry_at: datetime | None
    ) -> None: ...


class BrowserRuns(Protocol):
    async def start(self, job: BrowserJobLease, *, on: object) -> StartedRun | RefusedRun | None:
        """`on` is the session's profile lease (its lock's own connection). None: the attempt
        was started before (a redelivered job), and nothing may be touched."""
        ...

    async def finish(self, run_token: str, outcome: RunOutcome) -> bool: ...

    async def record_unapproved_read(
        self, job: BrowserJobLease, outcome: RunOutcome, *, on: object
    ) -> bool: ...


class LeaseLostError(Exception):
    """The job's lease expired or was taken: the run may be redelivered (never with its binding)."""


@dataclass
class Heartbeat:
    """Set by `lease_heartbeat`: whether a renewal failed while the run went on."""

    lost: bool = False
    beats: int = 0


@contextlib.asynccontextmanager
async def lease_heartbeat(
    queue: BrowserJobQueue, job: BrowserJobLease, *, every: timedelta, lease: timedelta
) -> AsyncIterator[Heartbeat]:
    """Renew the job's lease every `every` while the block runs (AC-8A.2.21), so a run longer
    than one lease period is not redelivered. A failed renewal stops the beat and marks it lost;
    the run's outcome is still recorded by its run token."""
    if every >= lease:
        raise ValueError("the heartbeat beats more often than the lease runs out")
    state = Heartbeat()

    async def beat() -> None:
        while True:
            await asyncio.sleep(every.total_seconds())
            try:
                await queue.renew(job, lease=lease)
            except LeaseLostError:
                state.lost = True
                _log.warning("browser.lease_lost")
                return
            except Exception as error:  # a failed beat is a lost beat; only the type is logged
                state.lost = True
                _log.warning("browser.lease_renewal_failed: %s", type(error).__name__)
                return
            state.beats += 1

    task = asyncio.ensure_future(beat())
    try:
        yield state
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
