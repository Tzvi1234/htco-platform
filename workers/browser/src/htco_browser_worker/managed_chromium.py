"""`ManagedChromiumProvider`: the only `BrowserProvider` (slice 8A.2a, ADR-0025 §1).

It is the existing code mapped onto the contract, not rewritten:
- **Provisioning:** 8A.1's `ProfileWorkspaces` seals a new, empty profile and deletes it.
- **Lifecycle:** one `ProfileWorkspaces.checkout` per session. `open` unpacks the last good
  archive into the tmpfs workspace and starts one browser on it. `save_session` stops the
  browser and re-seals the profile with the session's own lease checked inside the write
  (#169). `close` always stops the browser and wipes the workspace. `renew` is a health check
  followed by `save_session`; `recover` opens the last good archive in a fresh workspace, runs a
  health check and closes it without writing anything back.
- **Connection:** `PlaywrightEngine`'s persistent context, which Playwright starts on
  `--remote-debugging-pipe`. There is no TCP port, no loopback endpoint and no WebSocket URL.
- **Execution:** deterministic steps on that context (Playwright).
- **Monitoring:** a probe on `about:blank` that reads the running browser's version over the
  pipe, and the engine's own version.

**Parallel profiles.** It runs up to `parallel_profiles` sessions at once and refuses one more
(`ProviderAtCapacityError`). The controller hands out the slots, and its profile lock keeps one
profile from being opened twice; the capacity check is the provider's own guard.

The blocking profile work (unpack, pack, seal, wipe) runs in threads that always run to their
end. A cancellation that arrives meanwhile waits for them.
"""

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from htco_application.browser_profiles import ProfileFenceLostError, ProfileWorkspaces
from htco_application.browser_provider import (
    MANAGED_CHROMIUM,
    DeadlinePassedError,
    HandleClosedError,
    HealthReport,
    ProfileFence,
    ProviderAtCapacityError,
    ProviderCapabilities,
    ProviderHandle,
    ProviderLayer,
)
from htco_domain.browser import BrowserEnvironment

_log = logging.getLogger(__name__)

ENGINE_UNREACHABLE = "engine_unreachable"
PROFILE_UNUSABLE = "profile_unusable"
DEFAULT_PARALLEL_PROFILES = 2

# What this slice builds. Authentication (8A.4) and Networking (8A.3) join with their slices.
BUILT_LAYERS = frozenset(
    {
        ProviderLayer.PROVISIONING,
        ProviderLayer.LIFECYCLE,
        ProviderLayer.CONNECTION,
        ProviderLayer.EXECUTION,
        ProviderLayer.MONITORING,
    }
)


def managed_chromium_capabilities(
    engine_version: str, *, parallel_profiles: int = DEFAULT_PARALLEL_PROFILES
) -> ProviderCapabilities:
    """The declaration of `ManagedChromiumProvider` on a worker running `engine_version`."""
    return ProviderCapabilities(
        provider_id=MANAGED_CHROMIUM, engine="chromium", engine_version=engine_version,
        layers=BUILT_LAYERS, execution_engines=frozenset({"playwright"}), cdp_transport="pipe",
        renew=True, recover=True, downloads=False, parallel_profiles=parallel_profiles,
    )  # fmt: skip


# --- The engine it drives -----------------------------------------------------------------------


class EngineHandle(Protocol):
    async def close(self) -> None: ...


class BrowserEngine(Protocol):
    async def launch(self, profile_directory: Path) -> EngineHandle:
        """One browser on the unpacked profile, with its stock fingerprint (ADR-0020 §2)."""
        ...


@runtime_checkable
class ProbedHandle(Protocol):
    async def probe(self) -> str:
        """Load `about:blank`, run one script and return the browser's version over the pipe."""
        ...


@runtime_checkable
class VersionedEngine(Protocol):
    async def version(self) -> str: ...


# --- The provider -------------------------------------------------------------------------------


@dataclass(eq=False)
class ManagedHandle:
    """One session's browser and workspace. `engine` is None once the browser is stopped."""

    environment: BrowserEnvironment
    fence: ProfileFence = field(repr=False)
    checkout: AbstractContextManager[Path] = field(repr=False)
    directory: Path = field(repr=False)
    engine: EngineHandle | None = field(default=None, repr=False)
    finished: bool = False

    @property
    def environment_id(self) -> uuid.UUID:
        return self.environment.id


class ManagedChromiumProvider:
    def __init__(
        self,
        workspaces: ProfileWorkspaces,
        engine: BrowserEngine,
        capabilities: ProviderCapabilities,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspaces = workspaces
        self._engine = engine
        self._capabilities = capabilities
        self._clock = clock
        self._running_count = 0
        self._guard = threading.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    # Provisioning

    async def create_profile(self, environment: BrowserEnvironment) -> None:
        await _to_the_end(asyncio.to_thread(self._workspaces.create, environment))

    async def destroy_profile(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> None:
        _fence_for(fence, environment)
        await _to_the_end(asyncio.to_thread(self._workspaces.vault.delete, environment))

    # Lifecycle

    async def open(
        self, environment: BrowserEnvironment, *, deadline: datetime, fence: ProfileFence
    ) -> ManagedHandle:
        _fence_for(fence, environment)
        self._claim()
        try:
            handle = await self._unpacked(environment, fence)
        except BaseException:
            self._unclaim()
            raise
        # The unpack may have used up the time: never begin starting a browser past it.
        if deadline <= self._clock():
            await self.close(handle)
            raise DeadlinePassedError("the deadline passed during the unpack")
        try:
            handle.engine = await self._engine.launch(handle.directory)
        except BaseException:
            await self.close(handle)
            raise
        return handle

    async def save_session(self, handle: ProviderHandle, *, fence: ProfileFence) -> None:
        managed = self._live(handle)
        if fence is not managed.fence:
            raise ProfileFenceLostError("a session saves only under its own lease")
        await self._stop(managed)
        managed.finished = True
        try:
            # The checkout's clean exit: pack, seal under the fence, then wipe.
            await _to_the_end(asyncio.to_thread(managed.checkout.__exit__, None, None, None))
        finally:
            self._unclaim()

    async def renew(self, handle: ProviderHandle, *, fence: ProfileFence) -> HealthReport:
        report = await self.health_check(handle)
        if report.ok:
            await self.save_session(handle, fence=fence)
        return report

    async def recover(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> HealthReport:
        try:
            handle = await self.open(environment, deadline=datetime.max.replace(tzinfo=UTC),
                                     fence=fence)  # fmt: skip
        except ProviderAtCapacityError, ProfileFenceLostError:
            raise  # the fence and the slots are never a health result
        except Exception as error:
            # Only the error's type is logged, never its text.
            _log.warning("browser.recover_failed: %s", type(error).__name__)
            return HealthReport(ok=False, code=PROFILE_UNUSABLE)
        try:
            return await self.health_check(handle)
        finally:
            await self.close(handle)

    async def close(self, handle: ProviderHandle) -> None:
        managed = _managed(handle)
        if managed.finished:
            return
        managed.finished = True
        try:
            await self._stop(managed)
        finally:
            try:
                error = HandleClosedError("closed without saving")
                # An exit with an error never seals; it only wipes.
                await _to_the_end(
                    asyncio.to_thread(managed.checkout.__exit__, type(error), error, None)
                )
            finally:
                self._unclaim()

    # Connection and execution

    async def connect(self, handle: ProviderHandle) -> object:
        return self._running(handle)

    async def run_steps[T](
        self, handle: ProviderHandle, steps: Callable[[object], Awaitable[T]]
    ) -> T:
        return await steps(self._running(handle))

    # Monitoring

    async def health_check(self, handle: ProviderHandle) -> HealthReport:
        engine = self._running(handle)
        if not isinstance(engine, ProbedHandle):
            return HealthReport(ok=False, code=ENGINE_UNREACHABLE)
        try:
            version = await engine.probe()
        except Exception as error:
            _log.warning("browser.health_probe_failed: %s", type(error).__name__)
            return HealthReport(ok=False, code=ENGINE_UNREACHABLE)
        return HealthReport(ok=True, engine_version=version)

    async def engine_version(self) -> str:
        if isinstance(self._engine, VersionedEngine):
            return await self._engine.version()
        return self._capabilities.engine_version

    # Internals

    def _claim(self) -> None:
        with self._guard:
            if self._running_count >= self._capabilities.parallel_profiles:
                raise ProviderAtCapacityError("every profile slot is in use")
            self._running_count += 1

    def _unclaim(self) -> None:
        with self._guard:
            self._running_count -= 1

    async def _unpacked(
        self, environment: BrowserEnvironment, fence: ProfileFence
    ) -> ManagedHandle:
        checkout = self._workspaces.checkout(environment, fence=fence)
        entering = asyncio.ensure_future(asyncio.to_thread(checkout.__enter__))
        try:
            directory = await _to_the_end(entering)
        except BaseException as error:
            # Cancelled meanwhile: the unpack finished all the same, and is wiped again.
            if entering.done() and not entering.cancelled() and entering.exception() is None:
                await _to_the_end(
                    asyncio.to_thread(checkout.__exit__, type(error), error, error.__traceback__)
                )
            raise
        return ManagedHandle(environment, fence, checkout, directory)

    async def _stop(self, managed: ManagedHandle) -> None:
        engine, managed.engine = managed.engine, None
        if engine is not None:
            await _to_the_end(engine.close())

    def _live(self, handle: ProviderHandle) -> ManagedHandle:
        managed = _managed(handle)
        if managed.finished:
            raise HandleClosedError("the session's browser is saved or closed")
        return managed

    def _running(self, handle: ProviderHandle) -> EngineHandle:
        managed = self._live(handle)
        if managed.engine is None:
            raise HandleClosedError("the session's browser is not running")
        return managed.engine


def _managed(handle: ProviderHandle) -> ManagedHandle:
    if not isinstance(handle, ManagedHandle):
        raise TypeError("not a ManagedChromiumProvider handle")
    return handle


def _fence_for(fence: ProfileFence, environment: BrowserEnvironment) -> None:
    """A fence is this environment's held lease; anything else writes nothing."""
    if not fence.held or fence.environment_id != environment.id:
        raise ProfileFenceLostError("the lease does not hold this profile")


async def _to_the_end[T](work: Awaitable[T]) -> T:
    """Run `work` to its end even if the caller is cancelled meanwhile. A cancellation that
    arrived is raised once the work is done, and wins over the work's own error."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():  # pragma: no branch - only this module holds the task
            task.exception()
        raise asyncio.CancelledError
    return task.result()
