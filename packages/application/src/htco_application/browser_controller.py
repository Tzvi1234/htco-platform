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
from functools import partial
from pathlib import Path
from typing import Protocol

from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.browser_profiles import (
    ProfileFenceLostError,
    ProfileRecords,
    ProfileWorkspaces,
)
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
LOCK_LOST = "lock_lost"
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
    """The preflight found a problem (slice 8A.3); nothing was unpacked and no engine started."""

    def __init__(self, report: HealthReport) -> None:
        super().__init__(", ".join(report.problems))
        self.report = report


class PreflightTimeoutError(SessionTimeoutError):
    """The preflight ran past the session's deadline; no engine started."""


class SessionLockLostError(Exception):
    """The profile lock was lost during the session (its connection dropped); the profile was
    not written back, because another session may hold it now."""


class SessionRefusedError(Exception):
    """The environment's status takes no browser session."""


_FAILURES: dict[type[Exception], str] = {
    PreflightFailedError: PREFLIGHT_FAILED,
    PreflightTimeoutError: TIMEOUT,
    SessionLockLostError: LOCK_LOST,
}


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


class ProfileLease(Protocol):
    """One `hold()`'s claim on an environment's profile lock: `held` says whether it got the
    lock. It is bound to its own lock (in Postgres, its own connection), never to whichever
    session holds the environment now."""

    @property
    def environment_id(self) -> uuid.UUID: ...

    @property
    def held(self) -> bool: ...


class ProfileLocks(Protocol):
    def hold(
        self, environment_id: uuid.UUID, *, until: datetime
    ) -> AbstractAsyncContextManager[ProfileLease]:
        """Yields a lease that holds the environment's lock, or one with `held` false when the
        lock was still taken elsewhere at `until`. A held lease releases its lock on exit."""
        ...

    async def still_held(self, lease: ProfileLease) -> bool:
        """Whether this lease's own lock is still held: false once its connection dropped, even
        if another session has taken the environment's lock since."""
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
        environment = await self._takes_sessions(environment_id)
        record = SessionRecord(
            id=self._new_id(), environment_id=environment.id,
            organization_id=environment.organization_id, shop_id=environment.shop_id,
            purpose=purpose, started_at=self._clock(),
        )  # fmt: skip
        async with self._locks.hold(environment.id, until=deadline) as lease:
            if not lease.held or deadline <= self._clock():
                await self._start(record)
                await self._end(record, SessionOutcome.DEFERRED, None)
                raise SessionDeferredError("the profile is busy until past the deadline")
            # The status may have changed while this session waited for the lock.
            environment = await self._takes_sessions(environment.id)
            await self._start(record)
            outcome: SessionOutcome = SessionOutcome.FAILED
            failure: str | None = ERROR
            # Measured after the setup's database calls, so they count against the deadline.
            remaining = (deadline - self._clock()).total_seconds()
            limit = asyncio.timeout(remaining)
            try:
                if remaining <= 0:
                    failure = TIMEOUT
                    raise SessionTimeoutError("the deadline passed before the work could start")
                # The deadline covers the preflight, the engine and the work. Unpacking and the
                # clean exit's re-seal are bounded by the archive caps instead, so a session
                # that finished its work in time and committed its profile is never reported as
                # timed out. A failed preflight stops before anything is unpacked.
                # The session carries the status the preflight's record left (#170).
                environment = await self._check(environment, record, until=limit.when())
                async with self._profile(environment, lease) as directory, limit:
                    # The unpack may have used up the time: never begin starting a browser
                    # past the deadline.
                    if deadline <= self._clock():
                        failure = TIMEOUT
                        raise SessionTimeoutError("the deadline passed during the unpack")
                    handle = await self._engine.launch(directory)
                    try:
                        yield ControllerSession(record.id, environment, handle)
                    finally:
                        await _shielded(handle.close())
                    # Write the profile back only while this session's own lock holds.
                    await self._still_ours(lease)
                outcome, failure = SessionOutcome.SUCCEEDED, None
            except (PreflightFailedError, PreflightTimeoutError, SessionLockLostError) as error:
                failure = _FAILURES[type(error)]
                raise
            except ProfileFenceLostError:
                # The lock was lost between the check and the write: the fenced write refused.
                failure = LOCK_LOST
                raise SessionLockLostError("the profile lock was lost") from None
            except TimeoutError:
                if not limit.expired():
                    raise
                failure = TIMEOUT
                raise SessionTimeoutError("the session ran past its deadline") from None
            finally:
                await self._end(record, outcome, failure)

    async def destroy_profile(self, environment_id: uuid.UUID, *, deadline: datetime) -> None:
        """Delete the sealed profile under the profile lock, so no session that is still
        running can write it back afterwards (the environment is being deleted)."""
        environment = await _blocking(self._records.by_id, environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(str(environment_id))
        async with self._locks.hold(environment.id, until=deadline) as lease:
            if not lease.held:
                raise SessionDeferredError("the profile is busy until past the deadline")
            await _shielded(asyncio.to_thread(self._workspaces.vault.delete, environment))

    async def _check(
        self, environment: BrowserEnvironment, record: SessionRecord, *, until: float | None
    ) -> BrowserEnvironment:
        """The preflight, recorded as a health report, by the session's deadline (`until`, on
        the loop's clock); a failure stops the session here. Returns the environment with the
        status the record left, so the session never acts on the status from before it."""
        if self._preflight is None:
            return environment
        checking = asyncio.timeout_at(until)
        try:
            async with checking:
                report = await self._preflight.run()
        except TimeoutError:
            if not checking.expired():
                raise
            raise PreflightTimeoutError("the preflight ran past the deadline") from None
        if self._health is not None:
            status = await _blocking(
                self._health.record, environment, report,
                correlation_id=f"browser-session:{record.id}",
            )  # fmt: skip
            environment = replace(environment, status=status)
        if not report.ok:
            raise PreflightFailedError(report)
        return environment

    async def _still_ours(self, lease: ProfileLease) -> None:
        if not await self._locks.still_held(lease):
            raise SessionLockLostError("the profile lock was lost")

    async def _start(self, record: SessionRecord) -> None:
        """Record the start. The write runs to its end even if the task is cancelled meanwhile,
        and a record it wrote is then ended (failed) before the cancellation goes on, so no
        record is left `running`."""
        write = asyncio.ensure_future(asyncio.to_thread(self._log.started, record))
        try:
            await _shielded(write)
        except asyncio.CancelledError:
            if not write.cancelled() and write.exception() is None:
                await self._end(record, SessionOutcome.FAILED, ERROR)
            raise

    async def _takes_sessions(self, environment_id: uuid.UUID) -> BrowserEnvironment:
        environment = await _blocking(self._records.by_id, environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(str(environment_id))
        if environment.status not in _SESSION_STATUSES:
            raise SessionRefusedError(f"the environment is {environment.status.value}")
        return environment

    @asynccontextmanager
    async def _profile(
        self, environment: BrowserEnvironment, lease: ProfileLease
    ) -> AsyncIterator[Path]:
        """The workspace's checkout with its blocking work in threads that always run to
        their end, even under cancellation: unpack, then re-seal on success and wipe always.
        The re-seal is fenced by this session's own lease, checked inside the write (#169)."""
        checkout = self._workspaces.checkout(environment, fence=lease)
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

    async def _end(
        self, record: SessionRecord, outcome: SessionOutcome, failure: str | None
    ) -> None:
        await _shielded(
            asyncio.to_thread(
                partial(
                    self._log.ended, record, outcome=outcome, failure=failure, at=self._clock(),
                    count_error=outcome is SessionOutcome.FAILED,
                )
            )
        )  # fmt: skip


async def _blocking[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """A synchronous port call (a database adapter) off the event loop, so one slow query never
    stops other shops' sessions, timers or cleanup."""
    return await asyncio.to_thread(function, *args, **kwargs)


async def _shielded[T](work: Awaitable[T]) -> T:
    """Run cleanup to its end even if the session's task is cancelled meanwhile; a cancellation
    that arrived is raised once the cleanup is done, and wins over the cleanup's own error (the
    caller asked to stop)."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            # `asyncio.wait` never cancels the work it waits for (unlike awaiting it), and
            # unlike `asyncio.shield` it does not report the work's error as unhandled.
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():  # pragma: no branch - only we hold the work, never cancelled
            task.exception()  # retrieved: the cancellation is what goes on
        raise asyncio.CancelledError
    return task.result()


# --- In memory ----------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class MemoryLease:
    environment_id: uuid.UUID
    held: bool


@dataclass
class MemoryProfileLocks:
    """One holder per environment inside this process, polled like the Postgres lock. `held`
    is the environments locked now (a test drops an entry to play a dropped connection)."""

    held: set[uuid.UUID] = field(default_factory=set)
    _owners: dict[uuid.UUID, MemoryLease] = field(default_factory=dict)

    @asynccontextmanager
    async def hold(
        self, environment_id: uuid.UUID, *, until: datetime
    ) -> AsyncIterator[MemoryLease]:
        if not await self._wait(environment_id, until):
            yield MemoryLease(environment_id, held=False)
            return
        lease = MemoryLease(environment_id, held=True)
        self.held.add(environment_id)
        self._owners[environment_id] = lease
        try:
            yield lease
        finally:
            # Release only this lease's lock, never a later holder's.
            if self._owners.get(environment_id) is lease:
                del self._owners[environment_id]
                self.held.discard(environment_id)

    async def still_held(self, lease: ProfileLease) -> bool:
        return self.owns(lease, lease.environment_id)

    def owns(self, lease: object, environment_id: uuid.UUID) -> bool:
        """The fenced write's check (`MemoryProfileArchiveStore.fenced`): this very lease holds
        this environment's lock now."""
        return (
            isinstance(lease, MemoryLease)
            and lease.environment_id == environment_id
            and environment_id in self.held
            and self._owners.get(environment_id) is lease
        )

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
