"""`ManagedChromiumProvider` beyond the shared contract suite (slice 8A.2a): its failure codes,
its own guards, and blocking profile work that never stalls the event loop (AC-8A.2.9)."""

import asyncio
import itertools
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryLease,
    MemoryProfileLocks,
    MemorySessionLog,
)
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileFenceLostError,
    ProfileVault,
    ProfileWorkspaces,
)
from htco_application.browser_provider import HandleClosedError, ProviderAtCapacityError
from htco_browser_worker.managed_chromium import (
    ENGINE_UNREACHABLE,
    PROFILE_UNUSABLE,
    ManagedChromiumProvider,
    managed_chromium_capabilities,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio


def soon(seconds: float = 5.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def an_environment() -> BrowserEnvironment:
    return BrowserEnvironment(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
        status=EnvironmentStatus.READY,
    )  # fmt: skip


@dataclass
class Plain:
    """An engine handle with no health probe."""

    async def close(self) -> None:
        return None


@dataclass
class Probed(Plain):
    fails: bool = False

    async def probe(self) -> str:
        if self.fails:
            raise RuntimeError("page text that must never be logged")
        return "1.2.3"


class Engine:
    def __init__(self, handle: Plain) -> None:
        self.handle = handle

    async def launch(self, profile_directory: Path) -> Plain:  # noqa: ARG002
        return self.handle


class SlowWorkspaces(ProfileWorkspaces):
    """An unpack that blocks its thread (a large profile)."""

    @override
    @contextmanager
    def checkout(
        self, environment: BrowserEnvironment, *, fence: object | None = None
    ) -> Iterator[Path]:
        time.sleep(0.3)
        with super().checkout(environment, fence=fence) as directory:
            yield directory


def build(
    tmp_path: Path, handle: Plain, *, parallel: int = 2, slow: bool = False
) -> tuple[ManagedChromiumProvider, MemoryProfileLocks, ProfileVault]:
    locks = MemoryProfileLocks()
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))),
        MemoryProfileArchiveStore(fenced=locks.owns),
        lambda: datetime.now(UTC),
    )
    kind = SlowWorkspaces if slow else ProfileWorkspaces
    provider = ManagedChromiumProvider(
        kind(vault, tmp_path), Engine(handle),
        managed_chromium_capabilities("1.2.3", parallel_profiles=parallel),
    )  # fmt: skip
    return provider, locks, vault


async def test_health__an_engine_without_a_probe_is_unreachable(tmp_path: Path) -> None:
    provider, locks, _ = build(tmp_path, Plain())
    env = an_environment()
    async with locks.hold(env.id, until=soon()) as lease:
        handle = await provider.open(env, deadline=soon(), fence=lease)
        report = await provider.health_check(handle)
        await provider.close(handle)
    assert (report.ok, report.code) == (False, ENGINE_UNREACHABLE)


async def test_health__a_failed_probe_is_a_code_and_logs_no_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider, locks, _ = build(tmp_path, Probed(fails=True))
    env = an_environment()
    async with locks.hold(env.id, until=soon()) as lease:
        handle = await provider.open(env, deadline=soon(), fence=lease)
        report = await provider.renew(handle, fence=lease)  # a failed check writes nothing
        await provider.close(handle)
    assert (report.ok, report.code) == (False, ENGINE_UNREACHABLE)
    assert "page text" not in caplog.text


async def test_recover__an_archive_that_does_not_unpack_is_profile_unusable(
    tmp_path: Path,
) -> None:
    provider, locks, vault = build(tmp_path, Probed(), parallel=1)
    env, good = an_environment(), an_environment()
    vault.seal(env, b"not a gzip tar")
    async with locks.hold(env.id, until=soon()) as lease:
        report = await provider.recover(env, fence=lease)
    # The one slot it used is free again.
    async with locks.hold(good.id, until=soon()) as lease:
        handle = await provider.open(good, deadline=soon(), fence=lease)
        await provider.close(handle)
    assert (report.ok, report.code) == (False, PROFILE_UNUSABLE)
    assert not list(tmp_path.iterdir())  # noqa: ASYNC240 - the test's own check


async def test_save__only_under_the_lease_the_session_opened_with(tmp_path: Path) -> None:
    provider, locks, vault = build(tmp_path, Probed())
    env = an_environment()
    async with locks.hold(env.id, until=soon()) as lease:
        handle = await provider.open(env, deadline=soon(), fence=lease)
        with pytest.raises(ProfileFenceLostError):
            await provider.save_session(handle, fence=MemoryLease(env.id, held=True))
        await provider.save_session(handle, fence=lease)
        with pytest.raises(HandleClosedError):
            await provider.save_session(handle, fence=lease)
        await provider.close(handle)
    assert vault.open(env) is not None


async def test_destroy__needs_this_profiles_held_lease(tmp_path: Path) -> None:
    provider, locks, vault = build(tmp_path, Probed())
    env, other = an_environment(), an_environment()
    await provider.create_profile(env)
    async with locks.hold(other.id, until=soon()) as foreign:
        with pytest.raises(ProfileFenceLostError):
            await provider.destroy_profile(env, fence=foreign)
    assert vault.open(env) is not None
    async with locks.hold(env.id, until=soon()) as lease:
        await provider.destroy_profile(env, fence=lease)
    assert vault.open(env) is None


async def test_capacity__a_failed_open_gives_its_slot_back(tmp_path: Path) -> None:
    provider, locks, vault = build(tmp_path, Probed(), parallel=1)
    env = an_environment()
    vault.seal(env, b"not a gzip tar")
    async with locks.hold(env.id, until=soon()) as lease:
        for _ in range(3):
            with pytest.raises(Exception, match="unpack"):
                await provider.open(env, deadline=soon(), fence=lease)
    good = an_environment()
    async with locks.hold(good.id, until=soon()) as lease:
        handle = await provider.open(good, deadline=soon(), fence=lease)
        other = an_environment()
        async with locks.hold(other.id, until=soon()) as other_lease:
            with pytest.raises(ProviderAtCapacityError):
                await provider.open(other, deadline=soon(), fence=other_lease)
        await provider.close(handle)


@pytest.mark.ac("AC-8A.2.9")
async def test_async_ports__a_session_runs_while_the_heartbeat_keeps_its_schedule(
    tmp_path: Path,
) -> None:
    """The worker's heartbeat (its job lease) keeps beating through a session whose unpack and
    re-seal block for a while: the provider owns its thread hop, so nothing blocks the loop."""
    provider, locks, _ = build(tmp_path, Probed(), slow=True)
    env = an_environment()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}), provider, locks, MemorySessionLog(),
        clock=lambda: datetime.now(UTC),
    )  # fmt: skip
    beats: list[float] = []

    async def heartbeat() -> None:
        for _ in range(25):
            beats.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            await asyncio.sleep(0.05)

    await asyncio.gather(heartbeat(), run())
    longest = max(later - earlier for earlier, later in itertools.pairwise(beats))
    assert longest < 0.2  # the 0.3 s unpack ran in a thread
