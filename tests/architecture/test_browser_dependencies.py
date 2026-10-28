"""The browser dependency allowlist (Phase 8A security checklist, ADR-0020).

The browser worker's whole resolved dependency closure in `uv.lock` (its runtime dependencies,
their extras and everything they pull in, on every platform) must equal the reviewed list below.
Any new package fails the test until a reviewer adds it here: so no stealth plug-in, fingerprint
changer, anti-detect browser, proxy client (`python-socks`, `aiohttp-socks`, ...) or CAPTCHA
service can arrive unseen (Codex on #122). The denylist stays as a second guard over the whole
lock, and the browser engine is pinned to one exact version.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

_ROOT = "htco-browser-worker"

# The reviewed runtime closure of the browser worker. Adding a name here is a security review
# (SECURITY GATE items 8 and 10): say in the PR why the browser worker needs it.
_BROWSER_WORKER_CLOSURE = frozenset(
    {
        "annotated-types", "anyio", "certifi", "cffi", "cryptography", "greenlet", "h11",
        "htco-application", "htco-browser-worker", "htco-domain",
        "htco-platform", "httpcore", "httpx", "idna", "opentelemetry-api",
        "opentelemetry-instrumentation", "opentelemetry-instrumentation-httpx",
        "opentelemetry-sdk", "opentelemetry-semantic-conventions", "opentelemetry-util-http",
        "packaging", "playwright", "pycparser",
        "pydantic", "pydantic-core", "pydantic-settings", "pyee", "pyjwt", "python-dotenv",
        "sentry-sdk", "structlog", "typing-extensions", "typing-inspection",
        "urllib3", "wrapt",
    }
)  # fmt: skip
# Architecture audit 1.1: no database driver; htco-persistence and psycopg left the closure.

# Names (normalised) and name fragments that only an evasion or CAPTCHA-solving tool would have.
_FORBIDDEN = frozenset(
    {
        "playwright-stealth", "playwright-extra", "puppeteer-extra", "pyppeteer-stealth",
        "selenium-stealth", "undetected-chromedriver", "undetected-playwright", "nodriver",
        "camoufox", "browserforge", "fake-useragent", "fake-headers", "botasaurus",
        "patchright", "rebrowser-playwright", "zendriver", "seleniumbase", "cloudscraper",
        "curl-cffi", "tls-client", "2captcha-python", "anticaptchaofficial", "capsolver",
        "python-anticaptcha", "twocaptcha", "capmonster-python", "rotating-proxies",
        "proxy-rotator", "free-proxy", "adspower",
    }
)  # fmt: skip
_FORBIDDEN_FRAGMENTS = ("stealth", "captcha", "undetected", "antidetect", "anti-detect")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))


def _locked_packages() -> set[str]:
    return {_normalise(package["name"]) for package in _lock()["package"]}


def _closure(lock: dict[str, Any], root: str) -> set[str]:
    """Every package `root` needs at run time, following extras, over all of the lock's forks
    and platform markers (dev dependencies are not shipped, so they are not followed)."""
    entries: dict[str, list[dict[str, Any]]] = {}
    for package in lock["package"]:
        entries.setdefault(_normalise(package["name"]), []).append(package)
    seen: set[tuple[str, tuple[str, ...]]] = set()
    todo: list[tuple[str, tuple[str, ...]]] = [(root, ())]
    while todo:
        name, extras = todo.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        for entry in entries[name]:
            needs = list(entry.get("dependencies", []))
            for extra in extras:
                needs += entry.get("optional-dependencies", {}).get(extra, [])
            todo += [(_normalise(d["name"]), tuple(d.get("extra", []))) for d in needs]
    return {name for name, _ in seen}


def test_browser_worker__dependency_closure_is_the_reviewed_allowlist() -> None:
    closure = _closure(_lock(), _ROOT)
    assert "playwright" in closure  # the engine is there, so the check looks at the real tree
    unreviewed = sorted(closure - _BROWSER_WORKER_CLOSURE)
    assert not unreviewed, f"unreviewed browser-worker dependencies: {unreviewed}"
    # A name that left the closure leaves the list too, so the list never hides room.
    assert not sorted(_BROWSER_WORKER_CLOSURE - closure)


def test_allowlist__a_new_transitive_dependency_fails() -> None:
    socks = {"name": "Python_Socks", "extra": ["asyncio"]}
    lock = {
        "package": [
            {"name": _ROOT, "dependencies": [{"name": "playwright"}]},
            {"name": "playwright", "dependencies": [socks]},
            {
                "name": "python-socks",
                "optional-dependencies": {"asyncio": [{"name": "async-timeout"}]},
            },
            {"name": "async-timeout"},
        ]
    }
    closure = _closure(lock, _ROOT)
    assert closure == {_ROOT, "playwright", "python-socks", "async-timeout"}
    assert closure - _BROWSER_WORKER_CLOSURE == {"python-socks", "async-timeout"}


def test_lock__holds_no_stealth_proxy_or_captcha_package() -> None:
    names = _locked_packages()
    assert "playwright" in names
    assert names.isdisjoint(_FORBIDDEN)
    assert not [n for n in names if any(fragment in n for fragment in _FORBIDDEN_FRAGMENTS)]


def test_browser_worker__pins_the_engine_exactly() -> None:
    project = tomllib.loads(
        (REPO_ROOT / "workers/browser/pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = project["project"]["dependencies"]
    assert [d for d in dependencies if d.startswith("playwright")] == ["playwright==1.64.0"]


def test_detection__catches_a_forbidden_name() -> None:
    assert _normalise("Playwright_Stealth") in _FORBIDDEN
    assert any(fragment in "some-captcha-solver" for fragment in _FORBIDDEN_FRAGMENTS)
