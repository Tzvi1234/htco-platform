"""The Playwright engine (slice 8A.2): one Chromium persistent context on an unpacked profile.

`PlaywrightEngine` is the Browser Controller's `BrowserEngine`. It starts Playwright and opens
`chromium.launch_persistent_context(user_data_dir=<the unpacked profile>)`, so cookies and
storage live in the shop's own profile, which the controller re-seals after the session.

The browser is honest by default (ADR-0020 §2 and §4). The adapter has no way to set a user
agent, a proxy, a time zone, a coordinate or a screen, and it removes none of Playwright's
default switches. Chromium keeps its stock fingerprint, and the egress is the worker's own.

It passes only the typed options of the configuration profile (slice 8A.2b, ADR-0025 §2): the
platform baseline's viewport, color scheme, permissions (nothing granted unless a recipe's
scope row records a need) and pinned switch list, which turns off the browser's own background
traffic and Safe Browsing; and, from the shop's settings, its locale. Downloads stay off until a
recipe allows them (8A.5). The call in `launch` is the complete list of what is passed, and a
test pins it.

Starting and stopping Chromium are owned tasks that always run to their end (Codex on #122). A
cancellation that arrives meanwhile (the session's deadline, a shutdown) waits for them: a
browser that finished starting is closed again, and only then does the cancellation go on. So
the controller never re-seals the profile or releases its lock while Chromium is still alive.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from playwright.async_api import BrowserContext, Playwright, async_playwright

from htco_domain.browser_configuration import PLATFORM_BASELINE, LaunchSettings

LAUNCH_TIMEOUT_MS = 30_000

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaywrightHandle:
    """The running context; closing it closes Chromium and stops Playwright."""

    context: BrowserContext
    playwright: Playwright

    async def close(self) -> None:
        await _to_the_end(self._close())

    async def _close(self) -> None:
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

    async def launch(
        self, profile_directory: Path, *, settings: LaunchSettings | None = None
    ) -> PlaywrightHandle:
        """`settings`: the session's validated launch settings (`check_session`). Without
        them, the platform baseline alone, in the worker's own locale."""
        if settings is not None and settings.downloads:
            raise ValueError("downloads arrive with the recipes that declare them (8A.5)")
        opening = asyncio.ensure_future(self._open(profile_directory, settings))
        try:
            return await _to_the_end(opening)
        except asyncio.CancelledError:
            # Cancelled while Chromium started: it is closed before the cancellation goes on.
            if not opening.cancelled() and opening.exception() is None:
                try:
                    await _to_the_end(opening.result().close())
                except Exception as error:
                    # Never in place of the cancellation: the caller's deadline or shutdown is
                    # what goes on. Only the error's type is logged, never its text.
                    _log.warning("browser.close_failed_after_cancel: %s", type(error).__name__)
            raise

    async def _open(
        self, profile_directory: Path, settings: LaunchSettings | None
    ) -> PlaywrightHandle:
        playwright: Playwright = await self._start().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_directory), **launch_options(settings, self._honest())
            )
        except BaseException:
            await playwright.stop()
            raise
        return PlaywrightHandle(context, playwright)

    def _honest(self) -> dict[str, Any]:
        return {
            "headless": self._headless,
            "chromium_sandbox": self._sandbox,
            "timeout": self._timeout_ms,
            "accept_downloads": False,
        }


def launch_options(settings: LaunchSettings | None, engine: dict[str, Any]) -> dict[str, Any]:
    """Everything `launch_persistent_context` receives besides the profile directory."""
    baseline = PLATFORM_BASELINE if settings is None else settings
    options: dict[str, Any] = {
        **engine,
        "args": list(baseline.switches),
        "viewport": {"width": baseline.viewport.width, "height": baseline.viewport.height},
        "color_scheme": baseline.color_scheme,
        "permissions": [] if settings is None else sorted(settings.granted_permissions),
    }
    if settings is not None:
        # Sets the browser's language and its Accept-Language: the shop's real one.
        options["locale"] = settings.locale
    return options


async def _to_the_end[T](work: Awaitable[T]) -> T:
    """Run `work` to its end even if the caller is cancelled meanwhile. A cancellation that
    arrived is raised once the work is done, and wins over the work's own error."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            # `asyncio.wait` never cancels the task it waits for, unlike awaiting it.
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():  # pragma: no branch - only this module holds the task
            task.exception()  # retrieved: the cancellation is what goes on
        raise asyncio.CancelledError
    return task.result()
