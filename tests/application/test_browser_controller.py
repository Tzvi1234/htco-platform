"""The Browser Controller (slice 8A.2, ADR-0020 §3, `execution-engine.md` §3).

A session holds the environment's profile lock for its whole life, runs one engine on the
unpacked profile, and ends by its deadline. Whatever happens, the engine is closed, the profile
is wiped, the lock is released and the session is recorded; only a clean exit re-seals the
profile, so a crash keeps the last good archive.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryProfileLocks,
    MemorySessionLog,
    SessionDeferredError,
    SessionOutcome,
    SessionRefusedError,
    SessionTimeoutError,
)
from htco_application.browser_environments import EnvironmentNotFoundError
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileVault,
    ProfileWorkspaces,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio

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
        records, ProfileWorkspaces(vault, tmp_path), engine, locks, log, clock=now
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
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(s.vault, tmp_path), Broken(),
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
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(s.vault, tmp_path), SlowEngine(),
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
