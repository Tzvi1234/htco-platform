"""The Playwright engine (slice 8A.2): one honest Chromium persistent context per profile.

The unit tests use a fake Playwright and pin exactly what reaches Chromium: no user agent,
proxy, switches, locale, time zone or viewport (ADR-0020). The `browser` test runs a real
Chromium against a page served on 127.0.0.1 only (never a live site), through the Browser
Controller, and shows that a cookie survives from one sealed session to the next. It runs when
`HTCO_TEST_BROWSER=1` and Chromium is installed (`uv run playwright install chromium`).
"""

import asyncio
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryLease,
    MemoryProfileLocks,
    MemorySessionLog,
    SessionTimeoutError,
)
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileVault,
    ProfileWorkspaces,
)
from htco_browser_worker.engine import PlaywrightEngine, PlaywrightHandle
from htco_browser_worker.managed_chromium import (
    BrowserEngine,
    ManagedChromiumProvider,
    managed_chromium_capabilities,
)
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio


def _provider(workspaces: ProfileWorkspaces, engine: BrowserEngine) -> ManagedChromiumProvider:
    return ManagedChromiumProvider(workspaces, engine, managed_chromium_capabilities("test"))


# Anything here would change what the browser says about itself or where it comes from.
_DISHONEST = frozenset(
    {
        "user_agent", "proxy", "args", "ignore_default_args", "locale", "timezone_id",
        "geolocation", "viewport", "screen", "extra_http_headers", "device_scale_factor",
        "is_mobile", "has_touch", "executable_path", "channel", "env", "bypass_csp",
        "ignore_https_errors",
    }
)  # fmt: skip


@dataclass
class FakeContext:
    closed: bool = False
    fail_close: bool = False
    # Set: closing waits for it, as Chromium's shutdown takes time.
    gate: asyncio.Event | None = None
    events: list[str] = field(default_factory=list)

    async def close(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.closed = True
        self.events.append("context closed")
        if self.fail_close:
            raise RuntimeError("context close failed")


@dataclass
class FakeChromium:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False
    context: FakeContext = field(default_factory=FakeContext)
    # Set: the launch waits for it, as Chromium takes time to start.
    gate: asyncio.Event | None = None
    starting: asyncio.Event = field(default_factory=asyncio.Event)

    async def launch_persistent_context(self, user_data_dir: str, **options: Any) -> FakeContext:
        self.calls.append((user_data_dir, options))
        self.starting.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("chromium did not start")
        return self.context


@dataclass
class FakePlaywright:
    chromium: FakeChromium = field(default_factory=FakeChromium)
    stopped: bool = False

    async def start(self) -> FakePlaywright:
        return self

    async def stop(self) -> None:
        self.stopped = True
        self.chromium.context.events.append("playwright stopped")


def engine(fake: FakePlaywright) -> PlaywrightEngine:
    return PlaywrightEngine(start=lambda: fake)


async def test_launch__opens_the_profile_with_honest_options_only(tmp_path: Path) -> None:
    fake = FakePlaywright()
    handle = await engine(fake).launch(tmp_path)

    ((user_data_dir, options),) = fake.chromium.calls
    assert user_data_dir == str(tmp_path)
    assert options == {
        "headless": True, "chromium_sandbox": True, "timeout": 30_000, "accept_downloads": False,
    }  # fmt: skip
    assert not _DISHONEST & options.keys()
    assert repr(handle) == "PlaywrightHandle(<context>)"

    await handle.close()
    assert fake.chromium.context.closed
    assert fake.stopped


async def test_launch__a_failed_start_stops_playwright(tmp_path: Path) -> None:
    fake = FakePlaywright(chromium=FakeChromium(fail=True))
    with pytest.raises(RuntimeError, match="did not start"):
        await engine(fake).launch(tmp_path)
    assert fake.stopped


async def test_close__stops_playwright_even_when_the_context_fails_to_close(
    tmp_path: Path,
) -> None:
    fake = FakePlaywright(chromium=FakeChromium(context=FakeContext(fail_close=True)))
    handle = await engine(fake).launch(tmp_path)
    with pytest.raises(RuntimeError, match="close failed"):
        await handle.close()
    assert fake.stopped


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_launch__cancelled_while_chromium_starts_closes_it_before_cancelling(
    tmp_path: Path,
) -> None:
    """Codex on #122 (P1): a cancellation (the deadline, a shutdown) never cuts the launch off
    halfway and leaves a Chromium running. The launch finishes, the new context is closed and
    Playwright is stopped, and only then does the cancellation go on."""
    gate = asyncio.Event()
    fake = FakePlaywright(chromium=FakeChromium(gate=gate))
    task = asyncio.ensure_future(engine(fake).launch(tmp_path))
    await fake.chromium.starting.wait()
    task.cancel()
    await _settle()
    assert not task.done()  # still waiting for Chromium to finish starting

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.chromium.context.events == ["context closed", "playwright stopped"]


async def test_launch__cancelled_then_a_failing_close_still_raises_the_cancellation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex on #122 (local, 937369d): the cleanup's own error never replaces the cancellation,
    so a controller deadline still surfaces as a timeout. The close failure is logged instead."""
    gate = asyncio.Event()
    fake = FakePlaywright(chromium=FakeChromium(gate=gate, context=FakeContext(fail_close=True)))
    task = asyncio.ensure_future(engine(fake).launch(tmp_path))
    await fake.chromium.starting.wait()
    task.cancel()
    await _settle()
    gate.set()
    with (
        caplog.at_level("WARNING", logger="htco_browser_worker.engine"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert fake.chromium.context.events == ["context closed", "playwright stopped"]
    assert [r.getMessage() for r in caplog.records] == [
        "browser.close_failed_after_cancel: RuntimeError"
    ]


async def test_launch__cancelled_while_a_failing_start_runs_still_stops_playwright(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    fake = FakePlaywright(chromium=FakeChromium(gate=gate, fail=True))
    task = asyncio.ensure_future(engine(fake).launch(tmp_path))
    await fake.chromium.starting.wait()
    task.cancel()
    await _settle()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.chromium.context.events == ["playwright stopped"]


async def test_close__cancelled_midway_still_closes_chromium_and_stops_playwright(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    fake = FakePlaywright(chromium=FakeChromium(context=FakeContext(gate=gate)))
    handle = await engine(fake).launch(tmp_path)
    task = asyncio.ensure_future(handle.close())
    await _settle()
    task.cancel()
    await _settle()
    assert not task.done()  # Chromium is still shutting down

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.chromium.context.events == ["context closed", "playwright stopped"]


async def test_session__a_deadline_during_the_launch_releases_the_lock_after_chromium_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the Browser Controller: the deadline fires while Chromium starts. The session
    fails as a timeout, and the profile lock is released only after Chromium is closed and
    Playwright stopped, so no next session opens the profile under a live browser."""
    gate = asyncio.Event()
    fake = FakePlaywright(chromium=FakeChromium(gate=gate))
    events = fake.chromium.context.events
    env = BrowserEnvironment(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
        status=EnvironmentStatus.READY,
    )  # fmt: skip
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))),
        MemoryProfileArchiveStore(),
        lambda: datetime.now(UTC),
    )
    locks = MemoryProfileLocks()
    hold = locks.hold

    @asynccontextmanager
    async def recording_hold(
        environment_id: uuid.UUID, *, until: datetime
    ) -> AsyncIterator[MemoryLease]:
        try:
            async with hold(environment_id, until=until) as lease:
                yield lease
        finally:
            events.append("lock released")

    monkeypatch.setattr(locks, "hold", recording_hold)
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        _provider(ProfileWorkspaces(vault, tmp_path), engine(fake)), locks, MemorySessionLog(),
        clock=lambda: datetime.now(UTC),
    )  # fmt: skip

    async def run() -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=0.3)
        async with controller.session(env.id, purpose="test.run", deadline=deadline):
            pass  # pragma: no cover - the deadline passes during the launch

    task = asyncio.ensure_future(run())
    await fake.chromium.starting.wait()
    asyncio.get_running_loop().call_later(0.6, gate.set)  # Chromium starts after the deadline
    with pytest.raises(SessionTimeoutError):
        await task
    assert events == ["context closed", "playwright stopped", "lock released"]
    assert locks.held == set()


# --- A real Chromium against a local page ----------------------------------------------------


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>local fixture</title><p>HTCO test page</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path == "/sign-in":
            self.send_header("Set-Cookie", "htco_test=signed-in; Max-Age=3600; Path=/")
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def local_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
@pytest.mark.ac("AC-8A.2.2")
@pytest.mark.skipif(os.environ.get("HTCO_TEST_BROWSER") != "1", reason="HTCO_TEST_BROWSER=1")
async def test_chromium__a_cookie_survives_from_one_sealed_session_to_the_next(
    tmp_path: Path, local_site: str
) -> None:
    env = BrowserEnvironment(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
        status=EnvironmentStatus.READY,
    )  # fmt: skip
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))),
        MemoryProfileArchiveStore(),
        lambda: datetime.now(UTC),
    )
    root = tmp_path / "tmpfs"
    root.mkdir()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}),
        _provider(
            ProfileWorkspaces(vault, root),
            PlaywrightEngine(chromium_sandbox=os.environ.get("HTCO_TEST_BROWSER_SANDBOX") != "0"),
        ),
        MemoryProfileLocks(), MemorySessionLog(), clock=lambda: datetime.now(UTC),
    )  # fmt: skip

    def deadline() -> datetime:
        return datetime.now(UTC) + timedelta(seconds=60)

    async with controller.session(env.id, purpose="test.sign_in", deadline=deadline()) as one:
        assert isinstance(one.connection, PlaywrightHandle)
        page = await one.connection.context.new_page()
        await page.goto(f"{local_site}/sign-in")
        assert await page.title() == "local fixture"

    assert not any(root.iterdir())
    async with controller.session(env.id, purpose="test.again", deadline=deadline()) as two:
        assert isinstance(two.connection, PlaywrightHandle)
        cookies = await two.connection.context.cookies(local_site)
        assert [(c["name"], c["value"]) for c in cookies] == [("htco_test", "signed-in")]
