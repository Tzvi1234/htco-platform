"""The shared `BrowserProvider` contract suite (slice 8A.2a, ADR-0025 §1, AC-8A.2.4/5/8).

Every provider must pass it, layer by layer, against fixtures and `127.0.0.1` pages only, never a
live site. It runs for:
- `fake`: `FakeBrowserProvider`, written against the contract only;
- `managed`: `ManagedChromiumProvider` over a fake engine (its workspace, sealing and fence are
  real);
- `chromium`: `ManagedChromiumProvider` over a real Chromium (`HTCO_TEST_BROWSER=1`), which also
  checks that CDP runs on a pipe only.

A layer that a provider declares but this suite has no check for fails the suite: a declaration
is a claim, and an unconfirmed claim is refused (AC-8A.2.4).
"""

import asyncio
import os
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import parse_qs, urlsplit

import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryProfileLocks,
    MemorySessionLog,
    SessionDeferredError,
)
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileFenceLostError,
    ProfileVault,
    ProfileWorkspaces,
    SealedArchive,
)
from htco_application.browser_provider import (
    BrowserProvider,
    CapabilityDeclarationError,
    DeadlinePassedError,
    HandleClosedError,
    ProfileLease,
    ProviderAtCapacityError,
    ProviderCapabilities,
    ProviderHandle,
    ProviderLayer,
)
from htco_browser_worker.engine import PlaywrightEngine, PlaywrightHandle
from htco_browser_worker.managed_chromium import (
    ManagedChromiumProvider,
    ManagedHandle,
    managed_chromium_capabilities,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing
from tests.contracts.browser_provider.fake_provider import FakeBrowserProvider, FakeHandle

pytestmark = pytest.mark.anyio

# The layers this suite can confirm. Authentication (8A.4) and Networking (8A.3) join with the
# checks of their slices; until then a provider that declares them fails.
CONFIRMED_LAYERS = frozenset(
    {
        ProviderLayer.PROVISIONING,
        ProviderLayer.LIFECYCLE,
        ProviderLayer.CONNECTION,
        ProviderLayer.EXECUTION,
        ProviderLayer.MONITORING,
    }
)
_BROWSER = os.environ.get("HTCO_TEST_BROWSER") == "1"


def soon(seconds: float = 30.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def an_environment() -> BrowserEnvironment:
    return BrowserEnvironment(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
        status=EnvironmentStatus.READY,
    )  # fmt: skip


@dataclass
class Harness:
    """One provider under test, with a way to leave and read a marker in a session's profile
    (a cookie in a real Chromium, a file or a key elsewhere)."""

    name: str
    provider: BrowserProvider
    locks: MemoryProfileLocks
    write: Callable[[object, str], Awaitable[None]]
    read: Callable[[object], Awaitable[str | None]]
    directory: Callable[[ProviderHandle], Path | None] = lambda _: None
    # How many times a profile was written back so far (quill on #183: "writes nothing").
    writes: Callable[[], int] = lambda: 0
    # Makes the session's health check fail, where the harness can (quill on #183).
    break_check: Callable[[ProviderHandle], None] | None = None
    environments: list[BrowserEnvironment] = field(default_factory=list)

    def environment(self) -> BrowserEnvironment:
        env = an_environment()
        self.environments.append(env)
        return env


# --- The harnesses ------------------------------------------------------------------------------


def _fake(_: Path, __: str) -> Harness:
    locks = MemoryProfileLocks()

    async def write(connection: object, value: str) -> None:
        assert isinstance(connection, dict)
        connection["marker"] = value

    async def read(connection: object) -> str | None:
        assert isinstance(connection, dict)
        return connection.get("marker")

    provider = FakeBrowserProvider(locks.owns)

    def break_check(handle: ProviderHandle) -> None:
        assert isinstance(handle, FakeHandle)
        handle.healthy = False

    return Harness(
        "fake", provider, locks, write, read,
        writes=lambda: provider.saves, break_check=break_check,
    )  # fmt: skip


@dataclass
class FileHandle:
    profile: Path
    closed: bool = False
    broken: bool = False

    async def close(self) -> None:
        self.closed = True

    async def probe(self) -> str:
        if self.closed or self.broken:
            raise RuntimeError("closed")
        return "0.0.0-fake"


class FileEngine:
    async def launch(self, profile_directory: Path) -> FileHandle:
        return FileHandle(profile_directory)


@dataclass
class CountingStore(MemoryProfileArchiveStore):
    """The archives, counting every write (a re-seal of the same profile is still a write)."""

    puts: int = 0

    @override
    def put(self, archive: SealedArchive, *, fence: object | None = None) -> None:
        super().put(archive, fence=fence)
        self.puts += 1


def _workspaces(
    root: Path, locks: MemoryProfileLocks, store: CountingStore | None = None
) -> ProfileWorkspaces:
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))),
        store if store is not None else CountingStore(fenced=locks.owns),
        lambda: datetime.now(UTC),
    )
    root.mkdir(exist_ok=True)
    return ProfileWorkspaces(vault, root)


def _directory(handle: ProviderHandle) -> Path | None:
    return handle.directory if isinstance(handle, ManagedHandle) else None


def _managed(root: Path, _: str) -> Harness:
    locks = MemoryProfileLocks()
    store = CountingStore(fenced=locks.owns)
    provider = ManagedChromiumProvider(
        _workspaces(root / "tmpfs", locks, store), FileEngine(),
        managed_chromium_capabilities("0.0.0-fake"),
    )  # fmt: skip

    async def write(connection: object, value: str) -> None:
        assert isinstance(connection, FileHandle)
        (connection.profile / "marker").write_text(value, encoding="utf-8")

    async def read(connection: object) -> str | None:
        assert isinstance(connection, FileHandle)
        marker = connection.profile / "marker"
        return marker.read_text(encoding="utf-8") if marker.exists() else None

    def break_check(handle: ProviderHandle) -> None:
        assert isinstance(handle, ManagedHandle)
        assert isinstance(handle.engine, FileHandle)
        handle.engine.broken = True

    return Harness(
        "managed", provider, locks, write, read, _directory,
        writes=lambda: store.puts, break_check=break_check,
    )  # fmt: skip


def _real_engine() -> PlaywrightEngine:
    return PlaywrightEngine(chromium_sandbox=os.environ.get("HTCO_TEST_BROWSER_SANDBOX") != "0")


def _chromium(root: Path, site: str) -> Harness:
    locks = MemoryProfileLocks()
    # The declaration's version is filled in by the fixture, which can ask the engine.
    provider = ManagedChromiumProvider(
        _workspaces(root / "tmpfs", locks), _real_engine(), managed_chromium_capabilities("?")
    )

    async def write(connection: object, value: str) -> None:
        assert isinstance(connection, PlaywrightHandle)
        page = await connection.context.new_page()
        await page.goto(f"{site}/set?marker={value}")
        await page.close()

    async def read(connection: object) -> str | None:
        assert isinstance(connection, PlaywrightHandle)
        cookies = await connection.context.cookies(site)
        return next((c["value"] for c in cookies if c["name"] == "marker"), None)

    return Harness("chromium", provider, locks, write, read, _directory)


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        marker = parse_qs(url.query).get("marker")
        if url.path == "/set" and marker:
            self.send_header("Set-Cookie", f"marker={marker[0]}; Max-Age=3600; Path=/")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>contract fixture</title>")

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def local_site() -> Iterator[str]:
    """The test-only fixture origin on 127.0.0.1; no production configuration can load it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


_HARNESSES = [
    pytest.param(_fake, id="fake"),
    pytest.param(_managed, id="managed"),
    pytest.param(
        _chromium, id="chromium",
        marks=[pytest.mark.browser,
               pytest.mark.skipif(not _BROWSER, reason="HTCO_TEST_BROWSER=1")],
    ),
]  # fmt: skip


@pytest.fixture(params=_HARNESSES)
async def harness(request: pytest.FixtureRequest, tmp_path: Path, local_site: str) -> Harness:
    built: Harness = request.param(tmp_path, local_site)
    if built.name == "chromium":
        # The declaration names the build this worker really runs.
        assert isinstance(built.provider, ManagedChromiumProvider)
        version = await built.provider.engine_version()
        built.provider = ManagedChromiumProvider(
            _workspaces(tmp_path / "tmpfs", built.locks), _real_engine(),
            managed_chromium_capabilities(version),
        )  # fmt: skip
    for _ in range(built.provider.capabilities.parallel_profiles + 1):
        await built.provider.create_profile(built.environment())
    return built


async def session(
    h: Harness, env: BrowserEnvironment, lease: ProfileLease, marker: str | None = None
) -> ProviderHandle:
    handle = await h.provider.open(env, deadline=soon(), fence=lease)
    if marker is not None:
        await h.write(await h.provider.connect(handle), marker)
    return handle


async def marker_of(h: Harness, env: BrowserEnvironment) -> str | None:
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await h.provider.open(env, deadline=soon(), fence=lease)
        try:
            return await h.read(await h.provider.connect(handle))
        finally:
            await h.provider.close(handle)


# --- Capabilities -------------------------------------------------------------------------------


@pytest.mark.ac("AC-8A.2.4")
async def test_capabilities__every_declared_layer_is_one_the_suite_confirms(h: Harness) -> None:
    capabilities = h.provider.capabilities
    unconfirmed = capabilities.layers - CONFIRMED_LAYERS
    assert not unconfirmed, f"declared but not confirmed by the suite: {sorted(unconfirmed)}"
    assert capabilities.cdp_transport == "pipe"
    assert capabilities.parallel_profiles >= 1
    assert not capabilities.downloads  # nothing in this slice confirms downloads
    assert set(capabilities.execution_engines) <= {"playwright"}  # Browser Use is 8A.6


@pytest.fixture
def h(harness: Harness) -> Harness:
    return harness


@pytest.mark.ac("AC-8A.2.4")
@pytest.mark.parametrize(
    "change",
    [
        {"cdp_transport": "port"},
        {"parallel_profiles": 0},
        {"engine": "firefox"},
        {"engine_version": ""},
        {"layers": frozenset({ProviderLayer.LIFECYCLE})},
        {"execution_engines": frozenset({"selenium"})},
    ],
)
def test_capabilities__a_declaration_no_provider_may_make_is_refused(
    change: dict[str, object],
) -> None:
    good = managed_chromium_capabilities("1.0")
    fields = {name: getattr(good, name) for name in good.__dataclass_fields__} | change
    with pytest.raises(CapabilityDeclarationError):
        ProviderCapabilities(**fields)


def test_capabilities__a_declaration_is_not_complete_before_every_layer_exists() -> None:
    assert not managed_chromium_capabilities("1.0").complete


# --- Lifecycle ----------------------------------------------------------------------------------


@pytest.mark.ac("AC-8A.2.8")
async def test_lifecycle__a_saved_session_is_the_next_sessions_profile(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "first")
        await h.provider.save_session(handle, fence=lease)
        await h.provider.close(handle)
    assert await marker_of(h, env) == "first"


async def test_lifecycle__a_closed_session_that_was_not_saved_writes_nothing(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "unsaved")
        await h.provider.close(handle)
        await h.provider.close(handle)  # closing twice is harmless
        with pytest.raises(HandleClosedError):
            await h.provider.connect(handle)
    assert await marker_of(h, env) is None


@pytest.mark.ac("AC-8A.2.8")
async def test_fence__a_session_whose_lock_dropped_never_writes_back(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "good")
        await h.provider.save_session(handle, fence=lease)
        await h.provider.close(handle)
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "stale")
        h.locks.held.discard(env.id)  # the lock's connection drops
        with pytest.raises(ProfileFenceLostError):
            await h.provider.save_session(handle, fence=lease)
        await h.provider.close(handle)
    assert await marker_of(h, env) == "good"


@pytest.mark.ac("AC-8A.2.8")
async def test_fence__s1_drops_s2_takes_the_lock_and_only_s2_writes(h: Harness) -> None:
    """S1's lock connection drops and S2 takes the lock. S1's write-back is refused because its
    own lease is asked, never the environment's current holder."""
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as s1:
        first = await session(h, env, s1, "s1-stale")
        h.locks.held.discard(env.id)  # S1's connection drops
        async with h.locks.hold(env.id, until=soon()) as s2:
            second = await session(h, env, s2, "s2-fresh")
            with pytest.raises(ProfileFenceLostError):
                await h.provider.save_session(first, fence=s1)
            await h.provider.close(first)
            await h.provider.save_session(second, fence=s2)
            await h.provider.close(second)
    assert await marker_of(h, env) == "s2-fresh"


async def test_fence__open_refuses_a_lease_that_is_not_this_profiles(h: Harness) -> None:
    one, two = h.environments[0], h.environments[1]
    async with h.locks.hold(one.id, until=soon()) as lease:
        with pytest.raises(ProfileFenceLostError):
            await h.provider.open(two, deadline=soon(), fence=lease)
    async with (
        h.locks.hold(one.id, until=soon()),
        h.locks.hold(one.id, until=soon(0.05)) as waiting,
    ):
        assert not waiting.held
        with pytest.raises(ProfileFenceLostError):
            await h.provider.open(one, deadline=soon(), fence=waiting)


async def test_lifecycle__open_past_the_deadline_starts_nothing(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        with pytest.raises(DeadlinePassedError):
            await h.provider.open(env, deadline=datetime.now(UTC), fence=lease)
    # Nothing was left running: every slot is still free.
    await _fill_every_slot(h)


@pytest.mark.ac("AC-8A.2.8")
async def test_renew__reseals_under_the_fence_and_refuses_a_lost_one(h: Harness) -> None:
    if not h.provider.capabilities.renew:
        pytest.skip("renew is not declared")
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "renewed")
        report = await h.provider.renew(handle, fence=lease)
        assert report.ok
        await h.provider.close(handle)
    assert await marker_of(h, env) == "renewed"
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "lost")
        h.locks.held.discard(env.id)
        with pytest.raises(ProfileFenceLostError):
            await h.provider.renew(handle, fence=lease)
        await h.provider.close(handle)
    assert await marker_of(h, env) == "renewed"


@pytest.mark.ac("AC-8A.2.8")
async def test_recover__checks_the_last_good_archive_and_writes_nothing(h: Harness) -> None:
    if not h.provider.capabilities.recover:
        pytest.skip("recover is not declared")
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "last-good")
        await h.provider.save_session(handle, fence=lease)
        await h.provider.close(handle)
        crashed = await session(h, env, lease, "crashed-mid-way")
        await h.provider.close(crashed)  # a crash: never saved
        written = h.writes()
        report = await h.provider.recover(env, fence=lease)
        # quill on #183 (M8): recover checks the last good archive and writes nothing back.
        assert h.writes() == written
    assert report.ok
    assert report.engine_version == h.provider.capabilities.engine_version
    assert await marker_of(h, env) == "last-good"


@pytest.mark.ac("AC-8A.2.8")
async def test_renew__a_failed_check_writes_nothing_back(h: Harness) -> None:
    """quill on #183 (M9): renew re-seals only after a passing check; a failing one leaves the
    last good profile as it was."""
    if not h.provider.capabilities.renew:
        pytest.skip("renew is not declared")
    if h.break_check is None:
        pytest.skip("this harness cannot fail the check on purpose")
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "kept")
        await h.provider.save_session(handle, fence=lease)
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "unchecked")
        h.break_check(handle)
        written = h.writes()
        report = await h.provider.renew(handle, fence=lease)
        assert not report.ok
        assert h.writes() == written
        await h.provider.close(handle)
    assert await marker_of(h, env) == "kept"


# --- Connection and execution -------------------------------------------------------------------


@pytest.mark.ac("AC-8A.2.5")
async def test_connection__each_session_has_its_own_connection(h: Harness) -> None:
    one, two = h.environments[0], h.environments[1]
    async with (
        h.locks.hold(one.id, until=soon()) as lease_one,
        h.locks.hold(two.id, until=soon()) as lease_two,
    ):
        a = await session(h, one, lease_one, "only-in-a")
        b = await session(h, two, lease_two)
        try:
            a_connection = await h.provider.connect(a)
            b_connection = await h.provider.connect(b)
            assert a_connection is not b_connection
            assert await h.read(b_connection) is None
            assert await h.read(a_connection) == "only-in-a"
        finally:
            await h.provider.close(a)
            await h.provider.close(b)


async def test_execution__steps_run_on_the_sessions_connection(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease, "seen-by-the-steps")
        try:

            async def steps(connection: object) -> str | None:
                assert connection is await h.provider.connect(handle)
                return await h.read(connection)

            assert await h.provider.run_steps(handle, steps) == "seen-by-the-steps"
        finally:
            await h.provider.close(handle)


# --- Monitoring ---------------------------------------------------------------------------------


async def test_monitoring__the_health_check_reports_the_declared_engine(h: Harness) -> None:
    env = h.environments[0]
    async with h.locks.hold(env.id, until=soon()) as lease:
        handle = await session(h, env, lease)
        try:
            report = await h.provider.health_check(handle)
        finally:
            await h.provider.close(handle)
    assert report.ok
    assert report.engine_version == h.provider.capabilities.engine_version
    assert await h.provider.engine_version() == h.provider.capabilities.engine_version


# --- Parallel profiles --------------------------------------------------------------------------


@asynccontextmanager
async def _leases(
    h: Harness, environments: list[BrowserEnvironment]
) -> AsyncIterator[list[ProfileLease]]:
    async with AsyncExitStack() as stack:
        yield [
            await stack.enter_async_context(h.locks.hold(env.id, until=soon()))
            for env in environments
        ]


async def _fill_every_slot(h: Harness) -> None:
    capacity = h.provider.capabilities.parallel_profiles
    handles: list[ProviderHandle] = []
    async with _leases(h, h.environments[: capacity + 1]) as leases:
        try:
            for env, lease in list(zip(h.environments, leases, strict=False))[:capacity]:
                handles.append(await session(h, env, lease))
            with pytest.raises(ProviderAtCapacityError):
                await session(h, h.environments[capacity], leases[capacity])
            await h.provider.close(handles.pop())
            handles.append(await session(h, h.environments[capacity], leases[capacity]))
        finally:
            for handle in handles:
                await h.provider.close(handle)


@pytest.mark.ac("AC-8A.2.8")
async def test_parallel__the_declared_number_of_profiles_run_at_once_and_no_more(
    h: Harness,
) -> None:
    await _fill_every_slot(h)


@pytest.mark.ac("AC-8A.2.8")
async def test_parallel__through_the_controller_each_profile_once_under_its_own_lock(
    h: Harness,
) -> None:
    capacity = h.provider.capabilities.parallel_profiles
    records = MemoryProfileRecords({env.id: env for env in h.environments})
    controller = BrowserController(
        records, h.provider, h.locks, MemorySessionLog(), clock=lambda: datetime.now(UTC)
    )
    inside = 0
    most = 0
    all_in = asyncio.Event()
    deferred: list[Exception] = []

    async def run(env: BrowserEnvironment) -> None:
        nonlocal inside, most
        async with controller.session(env.id, purpose="test.parallel", deadline=soon()):
            inside += 1
            most = max(most, inside)
            assert env.id in h.locks.held  # its own lock
            if inside == capacity:
                all_in.set()
            await asyncio.wait_for(all_in.wait(), 10)
            await asyncio.sleep(0.2)
            inside -= 1

    async def twice(env: BrowserEnvironment) -> None:
        await all_in.wait()
        try:
            async with controller.session(env.id, purpose="test.twice", deadline=soon(0.1)):
                pytest.fail("one profile is never open twice")  # pragma: no cover
        except SessionDeferredError as error:
            deferred.append(error)

    await asyncio.gather(*(run(env) for env in h.environments[:capacity]),
                         twice(h.environments[0]))  # fmt: skip
    assert most == capacity
    assert len(deferred) == 1


# --- CDP on a pipe only (a real Chromium on Linux) ----------------------------------------------


def _processes() -> dict[int, tuple[int, list[str]]]:
    found: dict[int, tuple[int, list[str]]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        parent = int(stat.rsplit(")", 1)[1].split()[1])
        found[int(entry.name)] = (parent, [a.decode(errors="replace") for a in argv if a])
    return found


def _tree(processes: dict[int, tuple[int, list[str]]], root: int) -> set[int]:
    tree = {root}
    grew = True
    while grew:
        children = {pid for pid, (parent, _) in processes.items() if parent in tree}
        grew = not children <= tree
        tree |= children
    return tree


def _listening_inodes() -> set[str]:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            columns = line.split()
            if columns[3] == "0A":  # LISTEN
                inodes.add(columns[9])
    return inodes


def _socket_inodes(pids: set[int]) -> set[str]:
    inodes: set[str] = set()
    for pid in pids:
        try:
            descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = str(descriptor.readlink())
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[len("socket:[") : -1])
    return inodes


@pytest.mark.browser
@pytest.mark.ac("AC-8A.2.5")
@pytest.mark.skipif(not _BROWSER, reason="HTCO_TEST_BROWSER=1")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
async def test_chromium__cdp_runs_on_a_pipe_and_nothing_listens_for_it(
    tmp_path: Path, local_site: str
) -> None:
    locks = MemoryProfileLocks()
    provider = ManagedChromiumProvider(
        _workspaces(tmp_path / "tmpfs", locks), _real_engine(),
        managed_chromium_capabilities("pinned"),
    )  # fmt: skip
    one, two = an_environment(), an_environment()
    async with (
        locks.hold(one.id, until=soon()) as lease_one,
        locks.hold(two.id, until=soon()) as lease_two,
    ):
        a = await provider.open(one, deadline=soon(), fence=lease_one)
        b = await provider.open(two, deadline=soon(), fence=lease_two)
        try:
            connection = await provider.connect(a)
            assert isinstance(connection, PlaywrightHandle)
            page = await connection.context.new_page()
            await page.goto(local_site)
            processes = _processes()
            for handle in (a, b):
                directory = _directory(handle)
                assert directory is not None
                # The browser process itself: its children carry `--type=...`.
                mains = [pid for pid, (_, argv) in processes.items()
                         if f"--user-data-dir={directory}" in argv
                         and not any(arg.startswith("--type=") for arg in argv)]  # fmt: skip
                assert len(mains) == 1, "one browser per profile"
                argv = processes[mains[0]][1]
                assert "--remote-debugging-pipe" in argv
                assert not [arg for arg in argv if arg.startswith("--remote-debugging-port")]
                tree = _tree(processes, mains[0])
                assert not _socket_inodes(tree) & _listening_inodes(), "a browser listens on TCP"
        finally:
            await provider.close(a)
            await provider.close(b)
