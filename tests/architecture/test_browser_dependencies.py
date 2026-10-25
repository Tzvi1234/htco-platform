"""The browser dependency allowlist (Phase 8A security checklist, ADR-0020).

No stealth plug-in, fingerprint changer, anti-detect browser, proxy rotator or CAPTCHA service
may enter the dependency tree, and the browser engine is pinned to one exact version.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _locked_packages() -> set[str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_normalise(package["name"]) for package in lock["package"]}


def test_lock__holds_no_stealth_proxy_or_captcha_package() -> None:
    names = _locked_packages()
    assert "playwright" in names  # the engine is there, so the check looks at the real tree
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
