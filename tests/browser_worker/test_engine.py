"""The Playwright engine (slice 8A.2): one honest Chromium persistent context per profile.

The unit tests use a fake Playwright and pin exactly what reaches Chromium: no user agent,
proxy, switches, locale, time zone or viewport (ADR-0020). The `browser` test runs a real
Chromium against a page served on 127.0.0.1 only (never a live site), through the Browser
Controller, and shows that a cookie survives from one sealed session to the next. It runs when
`HTCO_TEST_BROWSER=1` and Chromium is installed (`uv run playwright install chromium`).
"""

import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryProfileLocks,
    MemorySessionLog,
)
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileVault,
    ProfileWorkspaces,
)
from htco_browser_worker.engine import PlaywrightEngine, PlaywrightHandle
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio

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

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("context close failed")


@dataclass
class FakeChromium:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False
    context: FakeContext = field(default_factory=FakeContext)

    async def launch_persistent_context(self, user_data_dir: str, **options: Any) -> FakeContext:
        self.calls.append((user_data_dir, options))
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
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(vault, root),
        PlaywrightEngine(chromium_sandbox=os.environ.get("HTCO_TEST_BROWSER_SANDBOX") != "0"),
        MemoryProfileLocks(), MemorySessionLog(), clock=lambda: datetime.now(UTC),
    )  # fmt: skip

    def deadline() -> datetime:
        return datetime.now(UTC) + timedelta(seconds=60)

    async with controller.session(env.id, purpose="test.sign_in", deadline=deadline()) as one:
        assert isinstance(one.engine, PlaywrightHandle)
        page = await one.engine.context.new_page()
        await page.goto(f"{local_site}/sign-in")
        assert await page.title() == "local fixture"

    assert not any(root.iterdir())
    async with controller.session(env.id, purpose="test.again", deadline=deadline()) as two:
        assert isinstance(two.engine, PlaywrightHandle)
        cookies = await two.engine.context.cookies(local_site)
        assert [(c["name"], c["value"]) for c in cookies] == [("htco_test", "signed-in")]
