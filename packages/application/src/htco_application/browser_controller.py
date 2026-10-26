"""The Browser Controller (slice 8A.2, ADR-0020 §3, `execution-engine.md` §3 and §8).

`BrowserController.session(environment_id, purpose=..., deadline=...)` is the only way to run a
browser on a shop's profile:

1. **One engine per profile.** It takes the environment's profile lock (`ProfileLocks`: a
   `pg_try_advisory_lock` on a dedicated connection in production) and holds it for the whole
   session. A second session for the same environment waits for it until its own deadline, then
   is recorded `DEFERRED` and refused (`SessionDeferredError`). Different environments never
   share a lock, so two shops run in parallel (AC-8A.2.1). If the worker dies, its connection
   drops and Postgres releases the lock (AC-8A.2.2).
2. **The profile.** It unpacks the sealed profile into the tmpfs workspace and starts one engine
   on it (`BrowserEngine`: Playwright's persistent context in production).
3. **The deadline.** The session ends by its deadline. Past it, the session is closed and
   recorded `FAILED (timeout)`, and the environment's error counter increments (AC-8A.2.3).
4. **Cleanup, always.** The engine is closed, the profile is wiped and the lock is released,
   whatever happens. Only a clean exit re-seals the profile, so a crashed or timed-out session
   keeps the last good archive and the next session starts from it (AC-8A.2.2).

Every session is recorded (`SessionLog`) with a stable purpose code and an outcome; a failure
is a code (`timeout`, `error`), never an exception's text, which could carry page content.

The application layer imports the standard library only, so the deadline is `asyncio.timeout`
and blocking profile work runs in `asyncio.to_thread`.
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.browser_profiles import ProfileRecords, ProfileWorkspaces
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus, HealthReport

_PURPOSE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_MAX_PURPOSE = 64
LOCK_POLL_SECONDS = 0.2
# A session runs on a profile that is being provisioned (the first health check), signed out
# (the person's sign-in), ready or degraded (the health checks that may restore it). A
# suspended, failed or deleted environment runs nothing until a person acts.
_SESSION_STATUSES = frozenset(
    {
        EnvironmentStatus.PROVISIONING,
        EnvironmentStatus.READY,
        EnvironmentStatus.LOGIN_REQUIRED,
        EnvironmentStatus.DEGRADED,
    }
)
TIMEOUT = "timeout"
PREFLIGHT_FAILED = "preflight_failed"
ERROR = "error"


class SessionOutcome(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"


class SessionDeferredError(Exception):
    """Another session holds the profile until past this session's deadline."""


class SessionTimeoutError(Exception):
    """The session ran past its deadline and was closed."""


class PreflightFailedError(Exception):
    """The preflight found a problem (slice 8A.3); no engine was started."""

    def __init__(self, report: HealthReport) -> None:
        super().__init__(", ".join(report.problems))
        self.report = report


class SessionRefusedError(Exception):
    """The environment's status takes no browser session."""


@dataclass(frozen=True)
class SessionRecord:
    id: uuid.UUID
    environment_id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    purpose: str
    started_at: datetime
    outcome: SessionOutcome = SessionOutcome.RUNNING
    failure: str | None = None
    ended_at: datetime | None = None


# --- Ports --------------------------------------------------------------------------------------


class ProfileLocks(Protocol):
    def hold(
        self, environment_id: uuid.UUID, *, until: datetime
    ) -> AbstractAsyncContextManager[bool]:
        """Yields True while this process holds the environment's lock, or False when it was
        still held elsewhere at `until`. The lock is released on exit."""
        ...


class EngineHandle(Protocol):
    async def close(self) -> None: ...


class BrowserEngine(Protocol):
    async def launch(self, profile_directory: Path) -> EngineHandle:
        """One browser on the unpacked profile, with its stock fingerprint (ADR-0020 §2)."""
        ...


class PreflightCheck(Protocol):
    async def run(self) -> HealthReport: ...


class HealthSink(Protocol):
    def record(
        self, environment: BrowserEnvironment, report: HealthReport, *, correlation_id: str
    ) -> EnvironmentStatus: ...


class SessionLog(Protocol):
    def started(self, record: SessionRecord) -> None: ...

    def ended(
        self,
        record: SessionRecord,
        *,
        outcome: SessionOutcome,
        failure: str | None,
        at: datetime,
        count_error: bool,
    ) -> None:
        """Record the end; with `count_error`, the environment's error counter increments."""
        ...


@dataclass(frozen=True)
class ControllerSession:
    id: uuid.UUID
    environment: BrowserEnvironment
    engine: EngineHandle


# --- The controller -----------------------------------------------------------------------------


class BrowserController:
    def __init__(  # noqa: PLR0913 - one port each
        self,
        records: ProfileRecords,
        workspaces: ProfileWorkspaces,
        engine: BrowserEngine,
        locks: ProfileLocks,
        log: SessionLog,
        *,
        clock: Callable[[], datetime],
        new_id: Callable[[], uuid.UUID] = uuid.uuid7,
        preflight: PreflightCheck | None = None,
        health: HealthSink | None = None,
    ) -> None:
        self._records = records
        self._workspaces = workspaces
        self._engine = engine
        self._locks = locks
        self._log = log
        self._clock = clock
        self._new_id = new_id
        self._preflight = preflight
        self._health = health

    @asynccontextmanager
    async def session(
        self, environment_id: uuid.UUID, *, purpose: str, deadline: datetime
    ) -> AsyncIterator[ControllerSession]:
        if not _PURPOSE.match(purpose) or len(purpose) > _MAX_PURPOSE:
            raise ValueError("a session's purpose is a short dotted code")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("a session's deadline is in UTC")
        environment = self._records.by_id(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(str(environment_id))
        if environment.status not in _SESSION_STATUSES:
            raise SessionRefusedError(f"the environment is {environment.status.value}")
        record = SessionRecord(
            id=self._new_id(), environment_id=environment.id,
            organization_id=environment.organization_id, shop_id=environment.shop_id,
            purpose=purpose, started_at=self._clock(),
        )  # fmt: skip
        async with self._locks.hold(environment.id, until=deadline) as held:
            remaining = (deadline - self._clock()).total_seconds()
            if not held or remaining <= 0:
                self._log.started(record)
                self._end(record, SessionOutcome.DEFERRED, None)
                raise SessionDeferredError("the profile is busy until past the deadline")
            # The status may have changed while this session waited for the lock.
            current = self._records.by_id(environment.id)
            if current is None or current.status not in _SESSION_STATUSES:
                raise SessionRefusedError("the environment changed while the session waited")
            environment = current
            self._log.started(record)
            outcome: SessionOutcome = SessionOutcome.FAILED
            failure: str | None = ERROR
            when = asyncio.get_running_loop().time() + remaining
            preflight_limit, limit = asyncio.timeout_at(when), asyncio.timeout_at(when)
            try:
                # The deadline covers the preflight, the engine and the work. Unpacking and the
                # clean exit's re-seal are bounded by the archive caps instead, so a session that
                # finished its work in time and committed its profile is never reported as
                # timed out. A failed preflight stops before anything is unpacked.
                async with preflight_limit:
                    await self._check(environment, record)
                async with self._profile(environment) as directory, limit:
                    handle = await self._engine.launch(directory)
                    try:
                        yield ControllerSession(record.id, environment, handle)
                    finally:
                        await _shielded(handle.close())
                outcome, failure = SessionOutcome.SUCCEEDED, None
            except PreflightFailedError:
                failure = PREFLIGHT_FAILED
                raise
            except TimeoutError:
                if not (limit.expired() or preflight_limit.expired()):
                    raise
                failure = TIMEOUT
                raise SessionTimeoutError("the session ran past its deadline") from None
            finally:
                self._end(record, outcome, failure)

    async def _check(self, environment: BrowserEnvironment, record: SessionRecord) -> None:
        """The preflight, recorded as a health report; a failure stops the session here."""
        if self._preflight is None:
            return
        report = await self._preflight.run()
        if self._health is not None:
            self._health.record(environment, report, correlation_id=f"browser-session:{record.id}")
        if not report.ok:
            raise PreflightFailedError(report)

    @asynccontextmanager
    async def _profile(self, environment: BrowserEnvironment) -> AsyncIterator[Path]:
        """The workspace's checkout with its blocking work in threads that always run to
        their end, even under cancellation: unpack, then re-seal on success and wipe always."""
        checkout = self._workspaces.checkout(environment)
        entering = asyncio.ensure_future(asyncio.to_thread(checkout.__enter__))
        try:
            # Cancelled meanwhile: the unpack still finishes, and is exited below, before the
            # caller releases the lock.
            directory = await _shielded(entering)
        except BaseException as error:
            if entering.done() and not entering.cancelled() and entering.exception() is None:
                await _shielded(
                    asyncio.to_thread(checkout.__exit__, type(error), error, error.__traceback__)
                )
            raise
        try:
            yield directory
        except BaseException as error:
            await _shielded(
                asyncio.to_thread(checkout.__exit__, type(error), error, error.__traceback__)
            )
            raise
        await _shielded(asyncio.to_thread(checkout.__exit__, None, None, None))

    def _end(self, record: SessionRecord, outcome: SessionOutcome, failure: str | None) -> None:
        self._log.ended(
            record, outcome=outcome, failure=failure, at=self._clock(),
            count_error=outcome is SessionOutcome.FAILED,
        )  # fmt: skip


async def _shielded[T](work: Awaitable[T]) -> T:
    """Run cleanup to its end even if the session's task is cancelled meanwhile; a cancellation
    that arrived is raised once the cleanup is done."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and task.cancelled():  # pragma: no cover - only we cancel it
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


# --- In memory ----------------------------------------------------------------------------------


@dataclass
class MemoryProfileLocks:
    """One holder per environment inside this process, polled like the Postgres lock."""

    held: set[uuid.UUID] = field(default_factory=set)

    @asynccontextmanager
    async def hold(self, environment_id: uuid.UUID, *, until: datetime) -> AsyncIterator[bool]:
        if not await self._wait(environment_id, until):
            yield False
        else:
            self.held.add(environment_id)
            try:
                yield True
            finally:
                self.held.discard(environment_id)

    async def _wait(self, environment_id: uuid.UUID, until: datetime) -> bool:
        while environment_id in self.held:
            left = (until - datetime.now(until.tzinfo)).total_seconds()
            if left <= 0:
                return False
            await asyncio.sleep(min(LOCK_POLL_SECONDS, left))
        return True


@dataclass
class MemorySessionLog:
    records: dict[uuid.UUID, SessionRecord] = field(default_factory=dict)
    errors: dict[uuid.UUID, int] = field(default_factory=dict)

    def started(self, record: SessionRecord) -> None:
        self.records[record.id] = record

    def ended(
        self,
        record: SessionRecord,
        *,
        outcome: SessionOutcome,
        failure: str | None,
        at: datetime,
        count_error: bool,
    ) -> None:
        self.records[record.id] = replace(
            self.records[record.id], outcome=outcome, failure=failure, ended_at=at
        )
        if count_error:
            self.errors[record.environment_id] = self.errors.get(record.environment_id, 0) + 1
