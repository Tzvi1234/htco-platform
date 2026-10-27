"""A fake `BrowserProvider` with no browser and no workspace: a profile is a dict of strings.

It is the shared contract suite's second implementation (AC-8A.2.4), written against the
contract only, so the suite cannot quietly depend on how `ManagedChromiumProvider` works. Its
fence is the memory lock's own answer (`MemoryProfileLocks.owns`), asked in the same step as
the write.
"""

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from htco_application.browser_profiles import ProfileFenceLostError
from htco_application.browser_provider import (
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

FAKE_VERSION = "1.0.0-fake"


def fake_capabilities(*, parallel_profiles: int = 2) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="fake", engine="chromium", engine_version=FAKE_VERSION,
        layers=frozenset({
            ProviderLayer.PROVISIONING, ProviderLayer.LIFECYCLE, ProviderLayer.CONNECTION,
            ProviderLayer.EXECUTION, ProviderLayer.MONITORING,
        }),
        execution_engines=frozenset({"playwright"}), cdp_transport="pipe", renew=True,
        recover=True, downloads=False, parallel_profiles=parallel_profiles,
    )  # fmt: skip


@dataclass(eq=False)
class FakeHandle:
    environment: BrowserEnvironment
    fence: ProfileFence = field(repr=False)
    state: dict[str, str] = field(default_factory=dict)
    running: bool = True
    finished: bool = False
    healthy: bool = True

    @property
    def environment_id(self) -> uuid.UUID:
        return self.environment.id


class FakeBrowserProvider:
    def __init__(
        self,
        fenced: Callable[[object, uuid.UUID], bool],
        capabilities: ProviderCapabilities | None = None,
        *,
        open_delay: float = 0.0,
    ) -> None:
        self._fenced = fenced
        self._capabilities = capabilities or fake_capabilities()
        self._open_delay = open_delay
        self.archives: dict[uuid.UUID, dict[str, str]] = {}
        self.saves = 0  # every write back, for the suite's "writes nothing" checks
        self.running = 0
        self._guard = threading.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def create_profile(self, environment: BrowserEnvironment) -> None:
        with self._guard:
            self.archives[environment.id] = {}

    async def destroy_profile(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> None:
        with self._guard:
            if not self._fenced(fence, environment.id):
                raise ProfileFenceLostError("the lease does not hold this profile")
            self.archives.pop(environment.id, None)

    async def open(
        self, environment: BrowserEnvironment, *, deadline: datetime, fence: ProfileFence
    ) -> FakeHandle:
        if not fence.held or fence.environment_id != environment.id:
            raise ProfileFenceLostError("the lease does not hold this profile")
        with self._guard:
            if self.running >= self._capabilities.parallel_profiles:
                raise ProviderAtCapacityError("every profile slot is in use")
            self.running += 1
            state = dict(self.archives.get(environment.id, {}))
        try:
            if self._open_delay:
                await asyncio.sleep(self._open_delay)
        except BaseException:
            self._release()
            raise
        if deadline <= datetime.now(UTC):
            self._release()
            raise DeadlinePassedError("the deadline passed during the unpack")
        return FakeHandle(environment, fence, state)

    def _release(self) -> None:
        with self._guard:
            self.running -= 1

    async def save_session(self, handle: ProviderHandle, *, fence: ProfileFence) -> None:
        fake = self._live(handle)
        fake.running = False
        fake.finished = True
        with self._guard:
            self.running -= 1
            # The fence is asked in the same step as the write.
            if fence is not fake.fence or not self._fenced(fence, fake.environment_id):
                raise ProfileFenceLostError("the session's lease no longer holds the profile")
            self.archives[fake.environment_id] = dict(fake.state)
            self.saves += 1

    async def renew(self, handle: ProviderHandle, *, fence: ProfileFence) -> HealthReport:
        report = await self.health_check(handle)
        if report.ok:
            await self.save_session(handle, fence=fence)
        return report

    async def recover(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> HealthReport:
        handle = await self.open(environment, deadline=datetime.max.replace(tzinfo=UTC),
                                 fence=fence)  # fmt: skip
        try:
            return await self.health_check(handle)
        finally:
            await self.close(handle)

    async def close(self, handle: ProviderHandle) -> None:
        fake = _fake(handle)
        if fake.finished:
            return
        fake.running = False
        fake.finished = True
        with self._guard:
            self.running -= 1

    async def connect(self, handle: ProviderHandle) -> object:
        return self._live(handle).state

    async def run_steps[T](
        self, handle: ProviderHandle, steps: Callable[[object], Awaitable[T]]
    ) -> T:
        return await steps(self._live(handle).state)

    async def health_check(self, handle: ProviderHandle) -> HealthReport:
        if not self._live(handle).healthy:
            return HealthReport(ok=False, code="engine_unreachable")
        return HealthReport(ok=True, engine_version=FAKE_VERSION)

    async def engine_version(self) -> str:
        return FAKE_VERSION

    def _live(self, handle: ProviderHandle) -> FakeHandle:
        fake = _fake(handle)
        if fake.finished or not fake.running:
            raise HandleClosedError("the session's browser is saved or closed")
        return fake


def _fake(handle: ProviderHandle) -> FakeHandle:
    if not isinstance(handle, FakeHandle):
        raise TypeError("not a fake provider handle")
    return handle
