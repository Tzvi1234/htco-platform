"""The Browser Controller (slice 8A.2, ADR-0020 §3, `execution-engine.md` §3).

A session holds the environment's profile lock for its whole life, runs one engine on the
unpacked profile, and ends by its deadline. Whatever happens, the engine is closed, the profile
is wiped, the lock is released and the session is recorded; only a clean exit re-seals the
profile, so a crash keeps the last good archive.
"""

import asyncio
import itertools
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import anyio
import pytest

from htco_application.browser_controller import (
    BrowserController,
    CapabilityRefusedError,
    MemoryLease,
    MemoryProfileLocks,
    MemorySessionLog,
    SessionDeferredError,
    SessionLockLostError,
    SessionOutcome,
    SessionRecord,
    SessionRefusedError,
    SessionTimeoutError,
)
from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileFenceLostError,
    ProfileUnsafeError,
    ProfileVault,
    ProfileWorkspaces,
    SealedArchive,
)
from htco_application.browser_provider import ProviderLayer, ProviderNeeds
from htco_browser_worker.managed_chromium import (
    BrowserEngine,
    ManagedChromiumProvider,
    managed_chromium_capabilities,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio


def provider(
    workspaces: ProfileWorkspaces, engine: BrowserEngine, *, parallel: int = 4
) -> ManagedChromiumProvider:
    """The controller drives `ManagedChromiumProvider` over a fake engine here; the real
    Chromium runs in `tests/browser_worker` and the shared contract suite."""
    capabilities = managed_chromium_capabilities("0.0.0-fake", parallel_profiles=parallel)
    return ManagedChromiumProvider(workspaces, engine, capabilities, clock=now)


COOKIE = b"controller-cookie"
ORG = uuid.UUID("0a000000-0000-0000-0000-000000000002")
_E = EnvironmentStatus


def now() -> datetime:
    return datetime.now(UTC)


def soon(seconds: float = 2.0) -> datetime:
    return now() + timedelta(seconds=seconds)


@dataclass
class Handle:
    engine: FakeEngine
    profile: Path
    closed: bool = False

    async def close(self) -> None:
        self.closed = True
        self.engine.running -= 1


@dataclass
class FakeEngine:
    """Counts engines per profile: two at once would be a second engine on one profile."""

    running: int = 0
    most: int = 0
    handles: list[Handle] = field(default_factory=list)

    async def launch(self, profile_directory: Path) -> Handle:
        self.running += 1
        self.most = max(self.most, self.running)
        handle = Handle(self, profile_directory)
        self.handles.append(handle)
        return handle


@dataclass
class Setup:
    controller: BrowserController
    engine: FakeEngine
    log: MemorySessionLog
    locks: MemoryProfileLocks
    vault: ProfileVault
    root: Path


def environment(status: EnvironmentStatus = _E.READY) -> BrowserEnvironment:
    return BrowserEnvironment(id=uuid.uuid4(), organization_id=ORG, shop_id=uuid.uuid4(),
                              status=status)  # fmt: skip


def setup(tmp_path: Path, *environments: BrowserEnvironment) -> Setup:
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), MemoryProfileArchiveStore(), now
    )
    records = MemoryProfileRecords({env.id: env for env in environments})
    engine, log, locks = FakeEngine(), MemorySessionLog(), MemoryProfileLocks()
    controller = BrowserController(
        records, provider(ProfileWorkspaces(vault, tmp_path), engine), locks, log, clock=now
    )
    return Setup(controller, engine, log, locks, vault, tmp_path)


def on_disk(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


async def test_session__runs_one_engine_on_the_profile_and_cleans_up(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)

    async with s.controller.session(env.id, purpose="health.check", deadline=soon()) as session:
        assert session.environment == env
        (s.engine.handles[0].profile / "Preferences").write_bytes(b"{}")
        assert s.locks.held == {env.id}

    assert s.engine.handles[0].closed
    assert s.locks.held == set()
    assert on_disk(tmp_path) == []
    assert s.vault.open(env) is not None  # re-sealed
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure, record.purpose) == (
        SessionOutcome.SUCCEEDED, None, "health.check",
    )  # fmt: skip
    assert record.ended_at is not None
    assert s.log.errors == {}


@pytest.mark.ac("AC-8A.2.1")
async def test_session__a_second_session_on_one_profile_waits_for_the_first(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    order: list[str] = []

    async def run(name: str, hold: float) -> None:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon(3)):
            order.append(f"{name} start")
            await anyio.sleep(hold)
            order.append(f"{name} end")

    async with anyio.create_task_group() as group:
        group.start_soon(run, "first", 0.3)
        await anyio.sleep(0.05)
        group.start_soon(run, "second", 0.0)

    assert order == ["first start", "first end", "second start", "second end"]
    assert s.engine.most == 1


@pytest.mark.ac("AC-8A.2.1")
async def test_session__a_second_session_past_its_deadline_is_deferred(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)
    first_in = anyio.Event()
    deferred: list[Exception] = []

    async def first() -> None:
        async with s.controller.session(env.id, purpose="test.long", deadline=soon(3)):
            first_in.set()
            await anyio.sleep(0.5)

    async def second() -> None:
        await first_in.wait()
        try:
            async with s.controller.session(env.id, purpose="test.short", deadline=soon(0.2)):
                pytest.fail("the second session must not start")  # pragma: no cover
        except SessionDeferredError as error:
            deferred.append(error)

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        group.start_soon(second)

    assert len(deferred) == 1
    outcomes = sorted(record.outcome for record in s.log.records.values())
    assert outcomes == sorted([SessionOutcome.SUCCEEDED, SessionOutcome.DEFERRED])
    assert s.engine.most == 1
    assert s.log.errors == {}


@pytest.mark.ac("AC-8A.2.1")
async def test_session__two_shops_run_in_parallel(tmp_path: Path) -> None:
    shop_a, shop_b = environment(), environment()
    s = setup(tmp_path, shop_a, shop_b)
    both_in = 0
    together = anyio.Event()

    async def run(env: BrowserEnvironment) -> None:
        nonlocal both_in
        async with s.controller.session(env.id, purpose="test.run", deadline=soon(2)):
            both_in += 1
            if both_in == 2:
                together.set()
            with anyio.fail_after(1):
                await together.wait()

    async with anyio.create_task_group() as group:
        group.start_soon(run, shop_a)
        group.start_soon(run, shop_b)

    assert s.engine.most == 2  # one engine per profile, two profiles


@pytest.mark.ac("AC-8A.2.2")
async def test_session__a_crash_releases_the_lock_and_keeps_the_last_good_archive(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    async with s.controller.session(env.id, purpose="test.good", deadline=soon()):
        (s.engine.handles[0].profile / "Cookies").write_bytes(b"good session")
    good = s.vault.open(env)

    async def crash() -> None:
        async with s.controller.session(env.id, purpose="test.crash", deadline=soon()):
            (s.engine.handles[1].profile / "Cookies").write_bytes(b"half written")
            raise RuntimeError("worker died")

    with pytest.raises(RuntimeError, match="worker died"):
        await crash()

    assert s.locks.held == set()
    assert s.engine.handles[1].closed
    assert on_disk(tmp_path) == []
    assert s.vault.open(env) == good
    async with s.controller.session(env.id, purpose="test.next", deadline=soon()):
        assert (s.engine.handles[2].profile / "Cookies").read_bytes() == b"good session"
    crashed = [r for r in s.log.records.values() if r.purpose == "test.crash"]
    assert [(r.outcome, r.failure) for r in crashed] == [(SessionOutcome.FAILED, "error")]
    assert s.log.errors == {env.id: 1}


@pytest.mark.ac("AC-8A.2.3")
async def test_session__past_its_deadline_is_closed_failed_timeout_and_counted(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    async with s.controller.session(env.id, purpose="test.good", deadline=soon()):
        pass
    good = s.vault.open(env)

    async def run_slow() -> None:
        async with s.controller.session(env.id, purpose="test.slow", deadline=soon(0.2)):
            (s.engine.handles[1].profile / "Cookies").write_bytes(b"late")
            await anyio.sleep(5)

    with pytest.raises(SessionTimeoutError):
        await run_slow()

    slow = [r for r in s.log.records.values() if r.purpose == "test.slow"]
    assert [(r.outcome, r.failure) for r in slow] == [(SessionOutcome.FAILED, "timeout")]
    assert s.log.errors == {env.id: 1}
    assert s.engine.handles[1].closed
    assert s.locks.held == set()
    assert on_disk(tmp_path) == []
    assert s.vault.open(env) == good


@pytest.mark.parametrize("status", [_E.SUSPENDED, _E.FAILED, _E.NOT_CONFIGURED])
async def test_session__refuses_an_environment_that_takes_no_session(
    tmp_path: Path, status: EnvironmentStatus
) -> None:
    env = environment(status)
    s = setup(tmp_path, env)
    with pytest.raises(SessionRefusedError):
        async with s.controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover - never entered
    assert s.engine.handles == []
    assert s.log.records == {}


async def test_session__refuses_an_unknown_environment_a_bad_purpose_or_deadline(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    with pytest.raises(EnvironmentNotFoundError):
        async with s.controller.session(uuid.uuid4(), purpose="test.run", deadline=soon()):
            pass  # pragma: no cover
    for purpose in ("", "Free text with spaces", "x" * 80):
        with pytest.raises(ValueError, match="purpose"):
            async with s.controller.session(env.id, purpose=purpose, deadline=soon()):
                pass  # pragma: no cover
    with pytest.raises(ValueError, match="deadline"):
        async with s.controller.session(env.id, purpose="test.run", deadline=datetime(2026, 1, 1)):  # noqa: DTZ001
            pass  # pragma: no cover
    with pytest.raises(SessionDeferredError):
        async with s.controller.session(env.id, purpose="test.run", deadline=now()):
            pass  # pragma: no cover
    assert s.engine.handles == []


async def test_session__an_engine_that_fails_to_start_is_a_failed_session(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)

    class Broken:
        async def launch(self, profile_directory: Path) -> Handle:  # noqa: ARG002
            raise OSError("chromium did not start")

    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), Broken()),
        s.locks, s.log, clock=now,
    )  # fmt: skip
    with pytest.raises(OSError, match="chromium"):
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "error")
    assert s.locks.held == set()
    assert on_disk(tmp_path) == []


async def test_session__a_timeout_raised_by_the_work_itself_is_not_the_deadline(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)

    async def run() -> None:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon()):
            raise TimeoutError("the page's own wait")

    with pytest.raises(TimeoutError, match="page's own wait"):
        await run()
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "error")


async def test_session__cleanup_finishes_even_when_the_task_is_cancelled_during_it(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    closing = asyncio.Event()
    closed = asyncio.Event()

    class SlowHandle:
        async def close(self) -> None:
            closing.set()
            await asyncio.sleep(0.2)
            closed.set()

    class SlowEngine:
        async def launch(self, profile_directory: Path) -> SlowHandle:  # noqa: ARG002
            return SlowHandle()

    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), SlowEngine()),
        s.locks, s.log, clock=now,
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass

    task = asyncio.ensure_future(run())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert s.locks.held == set()
    assert on_disk(tmp_path) == []


class SlowWorkspaces(ProfileWorkspaces):
    """A checkout whose unpack or re-seal takes a while (a large profile, a slow database)."""

    def __init__(
        self, vault: ProfileVault, root: Path, *, enter: float = 0, leave: float = 0
    ) -> None:
        super().__init__(vault, root)
        self._enter, self._leave = enter, leave
        self.unpacked = False

    @override
    @contextmanager
    def checkout(
        self, environment: BrowserEnvironment, *, fence: object | None = None
    ) -> Iterator[Path]:
        time.sleep(self._enter)
        with super().checkout(environment, fence=fence) as directory:
            self.unpacked = True
            yield directory
            time.sleep(self._leave)


@dataclass
class WatchedLocks(MemoryProfileLocks):
    """Notes, at the moment the lock is released, what is still on disk."""

    root: Path | None = None
    workspaces: SlowWorkspaces | None = None
    at_release: list[tuple[bool, list[Path]]] = field(default_factory=list)

    @override
    @asynccontextmanager
    async def hold(
        self, environment_id: uuid.UUID, *, until: datetime
    ) -> AsyncIterator[MemoryLease]:
        async with super().hold(environment_id, until=until) as lease:
            try:
                yield lease
            finally:
                assert self.root is not None
                assert self.workspaces is not None
                self.at_release.append((self.workspaces.unpacked, on_disk(self.root)))


def slow_controller(s: Setup, env: BrowserEnvironment, **delays: float) -> BrowserController:
    return BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(SlowWorkspaces(s.vault, s.root, **delays), s.engine),
        s.locks, s.log, clock=now,
    )  # fmt: skip


async def test_session__cancelled_during_unpack_cleans_up_before_the_lock_is_released(
    tmp_path: Path,
) -> None:
    """Codex on #121: the checkout thread is awaited and exited before the lock goes."""
    env = environment()
    s = setup(tmp_path, env)
    workspaces = SlowWorkspaces(s.vault, tmp_path, enter=0.3)
    locks = WatchedLocks(root=tmp_path, workspaces=workspaces)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}), provider(workspaces, s.engine), locks, s.log, clock=now
    )

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover - cancelled before

    task = asyncio.ensure_future(run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The lock went only after the unpack had finished and the profile was wiped.
    assert locks.at_release == [(True, [])]
    assert locks.held == set()
    assert s.engine.handles == []


async def test_session__rechecks_the_status_after_waiting_for_the_lock(tmp_path: Path) -> None:
    """Codex on #121: an environment suspended while a session waits never starts it."""
    env = environment()
    records = MemoryProfileRecords({env.id: env})
    s = setup(tmp_path, env)
    controller = BrowserController(
        records, provider(ProfileWorkspaces(s.vault, tmp_path), s.engine), s.locks, s.log, clock=now
    )
    first_in = asyncio.Event()
    refused: list[Exception] = []

    async def first() -> None:
        async with controller.session(env.id, purpose="test.first", deadline=soon()):
            first_in.set()
            await asyncio.sleep(0.2)  # the second session is waiting for the lock now
            records.environments[env.id] = env.transition(_E.SUSPENDED)

    async def second() -> None:
        await first_in.wait()
        try:
            async with controller.session(env.id, purpose="test.second", deadline=soon()):
                pytest.fail("a suspended environment must not start")  # pragma: no cover
        except SessionRefusedError as error:
            refused.append(error)

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        group.start_soon(second)
    assert len(refused) == 1
    assert len(s.engine.handles) == 1


async def test_session__a_clean_body_that_seals_past_the_deadline_is_a_success(
    tmp_path: Path,
) -> None:
    """Codex on #121: the outcome and the archive agree. The deadline covers the work; a clean
    exit's re-seal (bounded by the archive caps) commits the profile and is not a timeout."""
    env = environment()
    s = setup(tmp_path, env)
    controller = slow_controller(s, env, leave=0.4)
    async with controller.session(env.id, purpose="test.run", deadline=soon(0.2)):
        (s.engine.handles[0].profile / "Cookies").write_bytes(b"fresh")
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.SUCCEEDED, None)
    assert s.log.errors == {}
    assert on_disk(tmp_path) == []


async def test_session__an_archive_that_does_not_unpack_fails_and_releases_the_lock(
    tmp_path: Path,
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    s.vault.seal(env, b"not a gzip tar")

    async def run() -> None:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover - never entered

    with pytest.raises(ProfileUnsafeError):
        await run()
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "error")
    assert s.locks.held == set()
    assert on_disk(tmp_path) == []


@pytest.mark.ac("AC-8A.2.1")
async def test_destroy__waits_for_the_running_session_so_nothing_is_written_back(
    tmp_path: Path,
) -> None:
    """Codex on #121: destroying a profile takes the same lock, so a session that was running
    cannot seal its directory back after the archive is gone."""
    env = environment()
    s = setup(tmp_path, env)
    inside = asyncio.Event()

    async def session() -> None:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon()):
            inside.set()
            (s.engine.handles[0].profile / "Cookies").write_bytes(COOKIE)
            await asyncio.sleep(0.3)

    async with anyio.create_task_group() as group:
        group.start_soon(session)
        await inside.wait()
        await s.controller.destroy_profile(env.id, deadline=soon())

    assert s.vault.open(env) is None
    assert s.locks.held == set()


async def test_destroy__past_its_deadline_is_deferred(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)
    s.locks.held.add(env.id)  # a session elsewhere holds the profile
    with pytest.raises(SessionDeferredError):
        await s.controller.destroy_profile(env.id, deadline=soon(0.1))


@dataclass
class SlowLog(MemorySessionLog):
    """A session log on a slow database: each write blocks its thread."""

    delay: float = 0.3

    @override
    def started(self, record: SessionRecord) -> None:
        time.sleep(self.delay)
        super().started(record)


async def test_session__slow_storage_never_blocks_the_event_loop(tmp_path: Path) -> None:
    """Codex on #121: the controller's database calls run in threads, so one slow write does
    not stop other shops' sessions, timers or cleanup."""
    env = environment()
    s = setup(tmp_path, env)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), s.engine),
        s.locks, SlowLog(), clock=now,
    )  # fmt: skip
    beats: list[float] = []

    async def ticker() -> None:
        for _ in range(20):
            beats.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass

    await asyncio.gather(ticker(), run())
    longest = max(later - earlier for earlier, later in itertools.pairwise(beats))
    assert longest < 0.2  # the 0.3 s write ran in a thread, not on the loop


async def test_session__setup_time_counts_against_the_deadline(tmp_path: Path) -> None:
    """Codex on #121: time spent recording the start is inside the deadline; past it, no
    engine starts and the session is a timeout."""
    env = environment()
    s = setup(tmp_path, env)
    log = SlowLog(delay=0.3)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), s.engine),
        s.locks, log, clock=now,
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon(0.1)):
            pytest.fail("the work must not start past the deadline")  # pragma: no cover

    with pytest.raises(SessionTimeoutError):
        await run()
    assert s.engine.handles == []
    (record,) = log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "timeout")


async def test_destroy__an_unknown_environment_is_not_found(tmp_path: Path) -> None:
    s = setup(tmp_path, environment())
    with pytest.raises(EnvironmentNotFoundError):
        await s.controller.destroy_profile(uuid.uuid4(), deadline=soon())


async def test_session__a_lost_lock_never_writes_the_profile_back(tmp_path: Path) -> None:
    """quill on #121: if the lock's connection dropped during the session, another session may
    hold the profile now, so this one must not re-seal; it fails as `lock_lost`."""
    env = environment()
    s = setup(tmp_path, env)
    async with s.controller.session(env.id, purpose="test.good", deadline=soon()):
        (s.engine.handles[0].profile / "Cookies").write_bytes(b"good")
    good = s.vault.open(env)

    async def run() -> None:
        async with s.controller.session(env.id, purpose="test.lost", deadline=soon()):
            (s.engine.handles[1].profile / "Cookies").write_bytes(b"stale")
            s.locks.held.discard(env.id)  # the lock's connection dropped

    with pytest.raises(SessionLockLostError):
        await run()
    assert s.vault.open(env) == good
    assert s.engine.handles[1].closed
    assert on_disk(tmp_path) == []
    lost = [r for r in s.log.records.values() if r.purpose == "test.lost"]
    assert [(r.outcome, r.failure) for r in lost] == [(SessionOutcome.FAILED, "lock_lost")]


@pytest.mark.ac("AC-8A.2.2")
async def test_session__a_lock_lost_between_the_check_and_the_write_never_writes_back(
    tmp_path: Path,
) -> None:
    """Codex on #122 (#169): the lock drops after the fence check passed, while the profile is
    packed. The write itself takes the session's lease and refuses, so the stale profile never
    replaces the archive, and the session fails as `lock_lost`."""
    env = environment()
    locks = MemoryProfileLocks()

    class DropsDuringTheWrite(MemoryProfileArchiveStore):
        drop = False

        @override
        def put(self, archive: SealedArchive, *, fence: object | None = None) -> None:
            if self.drop:
                locks.held.discard(env.id)  # the lock's connection drops mid-write
            super().put(archive, fence=fence)

    store = DropsDuringTheWrite(fenced=locks.owns)
    vault = ProfileVault(EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), store, now)
    engine, log = FakeEngine(), MemorySessionLog()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(vault, tmp_path), engine), locks,
        log, clock=now,
    )  # fmt: skip
    async with controller.session(env.id, purpose="test.good", deadline=soon()):
        (engine.handles[0].profile / "Cookies").write_bytes(b"good")
    good = vault.open(env)

    store.drop = True
    with pytest.raises(SessionLockLostError):
        async with controller.session(env.id, purpose="test.stale", deadline=soon()):
            (engine.handles[1].profile / "Cookies").write_bytes(b"stale")
    assert vault.open(env) == good
    assert on_disk(tmp_path) == []
    stale = [r for r in log.records.values() if r.purpose == "test.stale"]
    assert [(r.outcome, r.failure) for r in stale] == [(SessionOutcome.FAILED, "lock_lost")]


async def test_store__a_fenced_write_needs_a_lease_that_still_holds() -> None:
    """The memory store's fence: a write with a lost or foreign lease writes nothing."""
    env = environment()
    locks = MemoryProfileLocks()
    store = MemoryProfileArchiveStore(fenced=locks.owns)
    vault = ProfileVault(EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), store, now)
    async with locks.hold(env.id, until=soon()) as lease:
        vault.seal(env, b"held", fence=lease)
        locks.held.discard(env.id)
        with pytest.raises(ProfileFenceLostError):
            vault.seal(env, b"stale", fence=lease)
    with pytest.raises(ProfileFenceLostError):
        vault.seal(env, b"after", fence=MemoryLease(env.id, held=True))
    assert vault.open(env) == b"held"


async def test_session__cancelled_while_recording_its_start_still_ends_the_record(
    tmp_path: Path,
) -> None:
    """Codex on #121: the start write finishes in its thread even if the task is cancelled
    meanwhile; the record it wrote is then ended (failed) before the lock is released, never
    left `running`."""
    env = environment()
    s = setup(tmp_path, env)
    log = SlowLog(delay=0.3)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), s.engine),
        s.locks, log, clock=now,
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover - cancelled before

    task = asyncio.ensure_future(run())
    await asyncio.sleep(0.1)  # inside the start write
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (record,) = log.records.values()
    assert record.outcome is SessionOutcome.FAILED
    assert record.ended_at is not None
    assert s.locks.held == set()
    assert s.engine.handles == []


@dataclass
class BrokenSlowLog(MemorySessionLog):
    """A start write that fails after a while (the database refused it)."""

    @override
    def started(self, record: SessionRecord) -> None:
        time.sleep(0.3)
        raise OSError("the database refused the write")


async def test_session__cancelled_while_a_failing_start_write_runs_ends_nothing(
    tmp_path: Path,
) -> None:
    """The failed write left no record, so nothing is ended; the cancellation goes on."""
    env = environment()
    s = setup(tmp_path, env)
    log = BrokenSlowLog()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        provider(ProfileWorkspaces(s.vault, tmp_path), s.engine),
        s.locks, log, clock=now,
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon()):
            pass  # pragma: no cover - cancelled before

    task = asyncio.ensure_future(run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log.records == {}
    assert s.locks.held == set()


@pytest.mark.ac("AC-8A.2.2")
async def test_session__a_lost_lock_taken_by_another_session_never_writes_back(
    tmp_path: Path,
) -> None:
    """sage and Codex on #121/#122: S1's lock drops and S2 takes the profile. S1's fence asks
    about S1's own lease, not about whoever holds the environment now, so S1 never seals its
    stale profile while S2 runs, and S1's exit leaves S2's lock alone."""
    env = environment()
    s = setup(tmp_path, env)
    async with s.controller.session(env.id, purpose="test.good", deadline=soon()):
        (s.engine.handles[0].profile / "Cookies").write_bytes(b"good")
    good = s.vault.open(env)
    s1_lost, s2_in, s1_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    results: list[str] = []

    async def first() -> None:
        try:
            async with s.controller.session(env.id, purpose="test.stale", deadline=soon(3)):
                (s.engine.handles[1].profile / "Cookies").write_bytes(b"stale")
                s.locks.held.discard(env.id)  # S1's lock connection drops
                s1_lost.set()
                await s2_in.wait()
        except SessionLockLostError:
            results.append("s1 lost its lock")
        finally:
            s1_done.set()

    async def second() -> None:
        await s1_lost.wait()
        async with s.controller.session(env.id, purpose="test.fresh", deadline=soon(3)):
            s2_in.set()
            await s1_done.wait()  # S1 finishes while S2 holds the profile
            assert s.vault.open(env) == good  # S1 did not write back
            assert s.locks.held == {env.id}  # and S1's exit did not release S2's lock
        results.append("s2 done")

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        group.start_soon(second)
    assert results == ["s1 lost its lock", "s2 done"]
    assert s.vault.open(env) == good


async def test_session__an_unpack_that_eats_the_deadline_never_starts_the_engine(
    tmp_path: Path,
) -> None:
    """sage and Codex on #122: the deadline is checked again right before the launch, so a slow
    unpack that used up the time never begins starting Chromium."""
    env = environment()
    s = setup(tmp_path, env)
    controller = slow_controller(s, env, enter=0.3)

    async def run() -> None:
        async with controller.session(env.id, purpose="test.run", deadline=soon(0.2)):
            pytest.fail("the work must not start past the deadline")  # pragma: no cover

    with pytest.raises(SessionTimeoutError):
        await run()
    assert s.engine.handles == []
    (record,) = s.log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "timeout")
    assert on_disk(tmp_path) == []


async def test_memory_locks__a_lease_answers_for_itself_only() -> None:
    """The in-memory locks keep the Postgres rule: a lease is held only while it is the
    environment's own holder, and leaving it never releases a later holder's lock."""
    locks = MemoryProfileLocks()
    environment_id = uuid.uuid4()
    async with locks.hold(environment_id, until=soon()) as first:
        assert await locks.still_held(first)
        async with locks.hold(environment_id, until=soon(0.05)) as waiter:
            assert not waiter.held
            assert not await locks.still_held(waiter)
        locks.held.discard(environment_id)  # the first lease's connection drops
        async with locks.hold(environment_id, until=soon()) as second:
            assert not await locks.still_held(first)
            assert await locks.still_held(second)
        assert locks.held == set()
    assert locks.held == set()


# --- 8A.2a: the provider contract under the controller -----------------------------------------


@pytest.mark.ac("AC-8A.2.4")
@pytest.mark.parametrize(
    ("needs", "missing"),
    [
        (
            ProviderNeeds(layers=frozenset({ProviderLayer.AUTHENTICATION})),
            ("layer:authentication",),
        ),
        (ProviderNeeds(execution_engines=frozenset({"browser_use"})), ("engine:browser_use",)),
        (ProviderNeeds(downloads=True), ("downloads",)),
    ],
)
async def test_session__a_need_the_provider_does_not_declare_is_refused_before_it_starts(
    tmp_path: Path, needs: ProviderNeeds, missing: tuple[str, ...]
) -> None:
    env = environment()
    s = setup(tmp_path, env)
    with pytest.raises(CapabilityRefusedError) as refused:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon(), needs=needs):
            pytest.fail("the session must not start")  # pragma: no cover
    assert refused.value.missing == missing
    # Refused before anything: no lock, no record, no engine.
    assert s.log.records == {}
    assert s.engine.handles == []
    assert s.locks.held == set()


async def test_session__a_declared_need_runs(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)
    needs = ProviderNeeds(layers=frozenset({ProviderLayer.MONITORING}), renew=True, recover=True)
    async with s.controller.session(env.id, purpose="test.run", deadline=soon(), needs=needs):
        pass
    (record,) = s.log.records.values()
    assert record.outcome is SessionOutcome.SUCCEEDED


def one_slot_setup(tmp_path: Path, *environments: BrowserEnvironment) -> Setup:
    s = setup(tmp_path, *environments)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env for env in environments}),
        provider(ProfileWorkspaces(s.vault, tmp_path), s.engine, parallel=1), s.locks, s.log,
        clock=now,
    )  # fmt: skip
    return Setup(controller, s.engine, s.log, s.locks, s.vault, tmp_path)


@pytest.mark.ac("AC-8A.2.8")
async def test_session__one_more_profile_than_the_capacity_waits_for_a_slot(
    tmp_path: Path,
) -> None:
    shop_a, shop_b = environment(), environment()
    s = one_slot_setup(tmp_path, shop_a, shop_b)
    order: list[str] = []

    async def run(name: str, env: BrowserEnvironment, hold: float) -> None:
        async with s.controller.session(env.id, purpose="test.run", deadline=soon(3)):
            order.append(f"{name} start")
            await anyio.sleep(hold)
            order.append(f"{name} end")

    async with anyio.create_task_group() as group:
        group.start_soon(run, "a", shop_a, 0.3)
        await anyio.sleep(0.05)
        group.start_soon(run, "b", shop_b, 0.0)

    assert order == ["a start", "a end", "b start", "b end"]
    assert s.engine.most == 1
    # The waiting session held its own profile's lock, and gave it back.
    assert s.locks.held == set()


@pytest.mark.ac("AC-8A.2.8")
async def test_session__no_slot_by_the_deadline_is_deferred_and_frees_its_lock(
    tmp_path: Path,
) -> None:
    shop_a, shop_b = environment(), environment()
    s = one_slot_setup(tmp_path, shop_a, shop_b)
    a_in = anyio.Event()
    deferred: list[Exception] = []

    async def first() -> None:
        async with s.controller.session(shop_a.id, purpose="test.long", deadline=soon(3)):
            a_in.set()
            await anyio.sleep(0.5)

    async def second() -> None:
        await a_in.wait()
        try:
            async with s.controller.session(shop_b.id, purpose="test.short", deadline=soon(0.2)):
                pytest.fail("no slot was free")  # pragma: no cover
        except SessionDeferredError as error:
            deferred.append(error)
            assert shop_b.id not in s.locks.held

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        group.start_soon(second)
    assert len(deferred) == 1
    short = [r for r in s.log.records.values() if r.purpose == "test.short"]
    assert [r.outcome for r in short] == [SessionOutcome.DEFERRED]
    assert s.log.errors == {}


@pytest.mark.ac("AC-8A.2.8")
async def test_destroy__goes_through_the_provider_under_the_lease(tmp_path: Path) -> None:
    env = environment()
    s = setup(tmp_path, env)
    async with s.controller.session(env.id, purpose="test.run", deadline=soon()):
        pass
    assert s.vault.open(env) is not None
    await s.controller.destroy_profile(env.id, deadline=soon())
    assert s.vault.open(env) is None
