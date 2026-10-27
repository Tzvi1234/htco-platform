"""The engine under a configuration profile (slice 8A.2b, AC-8A.2.11).

Unit tests pin the typed options a session's launch settings add: the locale, and nothing that
could make a shop look like another device. The `browser` tests run a real Chromium against
pages on 127.0.0.1 only (never a live site): two shops' sessions report the same User-Agent,
platform, time zone and viewport, and differ only in language; geolocation is denied; nothing
hides automation; and Chromium runs with the pinned switch list. They run with
`HTCO_TEST_BROWSER=1` and an installed Chromium (CI's `browser` job).
"""

import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from htco_browser_worker.engine import PlaywrightEngine, PlaywrightHandle, launch_options
from htco_domain.browser_configuration import (
    PINNED_SWITCHES,
    SHARED_EGRESS,
    LaunchSettings,
)
from tests.browser_worker.test_engine import _DISHONEST, FakePlaywright

pytestmark = pytest.mark.anyio

_REAL = pytest.mark.skipif(os.environ.get("HTCO_TEST_BROWSER") != "1", reason="HTCO_TEST_BROWSER=1")


def settings(locale: str = "he-IL", **overrides: Any) -> LaunchSettings:
    return LaunchSettings(locale=locale, egress_id=SHARED_EGRESS, **overrides)


@pytest.mark.ac("AC-8A.2.11")
async def test_launch__with_settings__adds_the_locale_and_nothing_else(tmp_path: Path) -> None:
    fake = FakePlaywright()
    handle = await PlaywrightEngine(start=lambda: fake).launch(tmp_path, settings=settings())

    ((_, options),) = fake.chromium.calls
    assert options == {
        "headless": True, "chromium_sandbox": True, "timeout": 30_000, "accept_downloads": False,
        "args": list(PINNED_SWITCHES), "viewport": {"width": 1280, "height": 900},
        "color_scheme": "light", "permissions": [], "locale": "he-IL",
    }  # fmt: skip
    assert not _DISHONEST & options.keys()
    await handle.close()


@pytest.mark.ac("AC-8A.2.11")
def test_launch_options__two_shops_differ_only_in_the_locale() -> None:
    engine = {"headless": True}
    one = launch_options(settings("he-IL"), engine)
    two = launch_options(settings("en-US"), engine)

    assert {k for k in one if one[k] != two[k]} == {"locale"}


@pytest.mark.ac("AC-8A.2.11")
def test_launch_options__geolocation_with_a_recorded_need__granted_with_no_coordinate() -> None:
    options = launch_options(
        settings(granted_permissions=frozenset({"geolocation"})), {"headless": True}
    )

    assert options["permissions"] == ["geolocation"]
    assert "geolocation" not in options  # the worker's real location, never a configured one


async def test_launch__downloads_are_refused_until_a_recipe_declares_them(tmp_path: Path) -> None:
    fake = FakePlaywright()
    with pytest.raises(ValueError, match=r"8A\.5"):
        await PlaywrightEngine(start=lambda: fake).launch(
            tmp_path, settings=settings(downloads=True)
        )
    assert fake.chromium.calls == []


# --- A real Chromium against local pages ------------------------------------------------------


class _Probe(BaseHTTPRequestHandler):
    seen_languages: list[str] = []  # noqa: RUF012 - per-server class below

    def do_GET(self) -> None:
        type(self).seen_languages.append(self.headers.get("Accept-Language", ""))
        body = b"<!doctype html><title>probe</title><p>HTCO configuration probe</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def probe_site() -> Iterator[tuple[str, list[str]]]:
    class Probe(_Probe):
        seen_languages: list[str] = []  # noqa: RUF012 - this server's own list

    handler = Probe
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler.seen_languages
    finally:
        server.shutdown()
        server.server_close()


_REPORT = """async () => {
  const geolocation = await new Promise((resolve) => {
    if (!navigator.geolocation) { resolve("unavailable"); return; }
    navigator.geolocation.getCurrentPosition(
      () => resolve("granted"), (error) => resolve("refused:" + error.code), {timeout: 3000});
  });
  return {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    width: window.innerWidth,
    height: window.innerHeight,
    language: navigator.language,
    webdriver: navigator.webdriver,
    light: window.matchMedia("(prefers-color-scheme: light)").matches,
    geolocationPermission: (await navigator.permissions.query({name: "geolocation"})).state,
    geolocation,
  };
}"""


async def _report(engine: PlaywrightEngine, profile: Path, launch: LaunchSettings, url: str) -> Any:
    profile.mkdir()  # noqa: ASYNC240 - a test fixture directory
    handle = await engine.launch(profile, settings=launch)
    try:
        assert isinstance(handle, PlaywrightHandle)
        page = await handle.context.new_page()
        await page.goto(url)
        return await page.evaluate(_REPORT), _chromium_command_lines(profile)
    finally:
        await handle.close()


def _chromium_command_lines(profile: Path) -> list[list[str]]:
    """The command lines of the processes that run this profile (Linux /proc only)."""
    if not Path("/proc/self/cmdline").exists():
        return []
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        if any(f"--user-data-dir={profile}" == argument for argument in arguments):
            found.append(arguments)
    return found


@pytest.mark.browser
@pytest.mark.ac("AC-8A.2.11")
@_REAL
async def test_chromium__two_shops_look_like_one_device_and_differ_only_in_language(
    tmp_path: Path, probe_site: tuple[str, list[str]]
) -> None:
    url, seen_languages = probe_site
    engine = PlaywrightEngine(chromium_sandbox=os.environ.get("HTCO_TEST_BROWSER_SANDBOX") != "0")

    hebrew, hebrew_lines = await _report(engine, tmp_path / "shop-a", settings("he-IL"), url)
    english, _ = await _report(engine, tmp_path / "shop-b", settings("en-US"), url)

    differing = {key for key in hebrew if hebrew[key] != english[key]}
    assert differing == {"language"}, json.dumps([hebrew, english])
    assert (hebrew["language"], english["language"]) == ("he-IL", "en-US")
    assert seen_languages[0].startswith("he-IL")
    assert seen_languages[1].startswith("en-US")
    # The pinned build's own User-Agent, the worker's time zone, the baseline's viewport.
    assert "Chrome/" in hebrew["userAgent"]
    assert hebrew["timeZone"]
    assert (hebrew["width"], hebrew["height"]) == (1280, 900)
    assert hebrew["light"] is True
    # Nothing hides automation, and geolocation is denied without a recorded need.
    assert hebrew["webdriver"] is True
    assert hebrew["geolocationPermission"] != "granted"
    assert hebrew["geolocation"] != "granted"
    # The pinned switch list is on Chromium's own command line (Linux /proc).
    if sys.platform.startswith("linux"):
        assert hebrew_lines, "no Chromium process found for the profile"
        # The browser process is the one without a `--type=` (renderers, GPU and utilities).
        browser = next(
            line for line in hebrew_lines if not any(a.startswith("--type=") for a in line)
        )
        assert set(PINNED_SWITCHES) <= set(browser)
        assert not any(arg.startswith("--remote-debugging-port") for arg in browser)
        assert not any("AutomationControlled" in arg for arg in browser)
