"""The Playwright engine (slice 8A.2): one Chromium persistent context on an unpacked profile.

`PlaywrightEngine` is the Browser Controller's `BrowserEngine`. It starts Playwright and opens
`chromium.launch_persistent_context(user_data_dir=<the unpacked profile>)`, so cookies and
storage live in the shop's own profile, which the controller re-seals after the session.

The browser is honest by default (ADR-0020 §2 and §4). The adapter has no way to set a user
agent, a proxy, extra command-line switches, a locale, a time zone, a geolocation or a
viewport, and it removes none of Playwright's default switches. Chromium keeps its stock
fingerprint, and the egress is the worker's own. Downloads are off until a recipe allows them
(8A.5). The call in `launch` is the complete list of what is passed, and a test pins it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from playwright.async_api import BrowserContext, Playwright, async_playwright

LAUNCH_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class PlaywrightHandle:
    """The running context; closing it closes Chromium and stops Playwright."""

    context: BrowserContext
    playwright: Playwright

    async def close(self) -> None:
        try:
            await self.context.close()
        finally:
            await self.playwright.stop()

    @override
    def __repr__(self) -> str:
        return "PlaywrightHandle(<context>)"


class PlaywrightEngine:
    def __init__(
        self,
        *,
        headless: bool = True,
        chromium_sandbox: bool = True,
        timeout_ms: int = LAUNCH_TIMEOUT_MS,
        start: Callable[[], Any] = async_playwright,
    ) -> None:
        self._headless = headless
        self._sandbox = chromium_sandbox
        self._timeout_ms = timeout_ms
        self._start = start

    async def launch(self, profile_directory: Path) -> PlaywrightHandle:
        playwright: Playwright = await self._start().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_directory),
                headless=self._headless,
                chromium_sandbox=self._sandbox,
                timeout=self._timeout_ms,
                accept_downloads=False,
            )
        except BaseException:
            await playwright.stop()
            raise
        return PlaywrightHandle(context, playwright)
