"""The three order automations are deterministic (AC-5.11.7): their handlers import nothing
from the agent runtime, the model gateway's application side or a browser library. The
import-linter contract `order-automations-deterministic` covers the packages; this test also
covers the application's own agent modules, which a layer contract cannot single out."""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLERS = [
    REPO_ROOT / "packages" / "application" / "src" / "htco_application" / name
    for name in ("order_intake.py", "order_automations.py", "order_ingest.py")
] + [REPO_ROOT / "packages" / "persistence" / "src" / "htco_persistence" / "order_automations.py"]
FORBIDDEN = (
    "htco_agent_runtime",
    "htco_application.agents",
    "htco_application.decisions",
    "langgraph",
    "langchain_core",
    "browser_use",
    "playwright",
    "anthropic",
    "openai",
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append(node.module)
    return found


@pytest.mark.ac("AC-5.11.7")
@pytest.mark.parametrize("path", HANDLERS, ids=lambda p: f"{p.parent.parent.parent.name}-{p.stem}")
def test_order_automation_handlers__no_model_agent_or_browser_import(path: Path) -> None:
    bad = [m for m in _imports(path) if m.startswith(FORBIDDEN)]

    assert bad == [], f"{path.name} imports {bad}"
