"""No business code reaches a browser provider or Chromium (slice 8A.2a, ADR-0025 §1, AC-8A.2.7).

import-linter forbids `playwright` and `browser_use` outside the browser worker, and the model
SDKs outside the gateway package. A forbidden module that is not installed (Browser Use, the
SDKs today) gives import-linter nothing to see, so this test reads the sources themselves.

Business logic depends on `BrowserController` and the `BrowserProvider` port only. The domain,
the application layer, the agent runtime (the graphs), the API, the worker (the execution engine,
the gateway, the recipes and the guards), persistence and the integrations never import a
provider class, the browser-worker package, Playwright or Browser Use.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUSINESS_SOURCES = (
    "packages/domain/src",
    "packages/application/src",
    "packages/agent-runtime/src",
    "packages/persistence/src",
    "packages/integrations/src",
    "packages/platform/src",
    "apps/api/src",
    "workers/runner/src",
)
_BROWSER_ENGINES = ("playwright", "browser_use", "htco_browser_worker")
_MODEL_SDKS = ("anthropic", "openai", "langchain_anthropic", "langchain_openai", "litellm")
_PROVIDER_CLASSES = ("ManagedChromiumProvider", "PlaywrightEngine", "PlaywrightHandle")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
            found += [f"{node.module}.{alias.name}" for alias in node.names]
    return found


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def _sources(roots: tuple[str, ...]) -> list[Path]:
    files = [path for root in roots for path in (REPO_ROOT / root).rglob("*.py")]
    assert files, "the scan found no sources"
    return files


def _top(module: str) -> str:
    return module.split(".", 1)[0]


@pytest.mark.ac("AC-8A.2.7")
def test_business_code__imports_no_provider_chromium_or_browser_use() -> None:
    offending = [
        f"{path.relative_to(REPO_ROOT)}: {module}"
        for path in _sources(_BUSINESS_SOURCES)
        for module in _imports(path)
        if _top(module) in _BROWSER_ENGINES
    ]
    assert not offending


@pytest.mark.ac("AC-8A.2.7")
def test_business_code__names_no_provider_class() -> None:
    offending = [
        f"{path.relative_to(REPO_ROOT)}: {name}"
        for path in _sources(_BUSINESS_SOURCES)
        for name in sorted(_names(path) & set(_PROVIDER_CLASSES))
    ]
    assert not offending


@pytest.mark.ac("AC-8A.2.7")
def test_model_sdks__are_imported_nowhere_until_the_gateway_package_exists() -> None:
    offending = [
        f"{path.relative_to(REPO_ROOT)}: {module}"
        for path in _sources((*_BUSINESS_SOURCES, "workers/browser/src"))
        for module in _imports(path)
        if _top(module) in _MODEL_SDKS
    ]
    assert not offending


def test_scan__catches_a_forbidden_import(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text(
        "import browser_use\nfrom playwright.async_api import Page\n"
        "from htco_browser_worker.managed_chromium import ManagedChromiumProvider\n"
        "x = ManagedChromiumProvider\n",
        encoding="utf-8",
    )
    tops = {_top(module) for module in _imports(source)}
    assert {"browser_use", "playwright", "htco_browser_worker"} <= tops
    assert "ManagedChromiumProvider" in _names(source)
