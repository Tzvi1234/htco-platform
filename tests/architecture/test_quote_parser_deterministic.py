"""AC-7.15.2: the AutoVA quote parser and its intake import nothing from a model provider, the
model gateway or the agent runtime. The import contract `quote-parser-deterministic` covers the
packages import-linter knows; this test also refuses any provider SDK by name, installed or not,
and any import outside the standard library and HTCO's own inner layers."""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    ROOT / "packages/application/src/htco_application/quote_parser.py",
    ROOT / "packages/application/src/htco_application/suppliers.py",
]
FORBIDDEN = ("anthropic", "openai", "google.genai", "litellm", "langgraph", "langchain")
ALLOWED_OWN = ("htco_domain", "htco_application.kernel", "htco_application.quote_parser")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.append(node.module)
    return found


@pytest.mark.ac("AC-7.15.2")
@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_parser_imports_no_model_or_agent_code(path: Path) -> None:
    imports = _imports(path)
    assert not [m for m in imports if m.startswith(FORBIDDEN) or "model" in m.split(".")[-1]]
    foreign = [
        m
        for m in imports
        if m.split(".")[0] not in sys.stdlib_module_names and not m.startswith(ALLOWED_OWN)
    ]
    assert foreign == [], f"{path.name} imports {foreign}"
