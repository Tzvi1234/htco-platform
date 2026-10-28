"""Architecture contract (7.2, sage on #189): the policy engine is reached only through
`PolicyService.decide`, which records every decision with its policy version. A caller that
imported `access.engine.evaluate` directly would get a decision nobody recorded, so outside the
tests only `htco_application.policies` may import the engine."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = sorted(
    [
        *REPO_ROOT.glob("packages/*/src"),
        *REPO_ROOT.glob("apps/*/src"),
        *REPO_ROOT.glob("workers/*/src"),
        REPO_ROOT / "tools",
    ]
)
ENGINE = "htco_application.access.engine"
ENGINE_FILE = "packages/application/src/htco_application/access/engine.py"
ALLOWED = {"packages/application/src/htco_application/policies.py"}


def _imports_the_engine(tree: ast.AST, module_of_file: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # a relative import, resolved against the file's package
                base = module_of_file.rsplit(".", node.level)[0]
                module = f"{base}.{module}" if module else base
            if module == ENGINE or (
                module == "htco_application.access"
                and any(alias.name == "engine" for alias in node.names)
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == ENGINE or alias.name.startswith(f"{ENGINE}.") for alias in node.names
        ):
            return True
    return False


def importers_of_the_engine() -> set[str]:
    found: set[str] = set()
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == ENGINE_FILE:
                continue
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            if _imports_the_engine(ast.parse(path.read_text(encoding="utf-8")), module):
                found.add(relative)
    return found


def test_only_the_policy_service_imports_the_engine() -> None:
    assert importers_of_the_engine() == ALLOWED


def test_the_contract_sees_a_direct_import() -> None:
    """The check itself: each way of importing the engine is caught."""
    for source in (
        "from htco_application.access.engine import evaluate",
        "from htco_application.access import engine",
        "import htco_application.access.engine",
        "from .engine import evaluate",
    ):
        assert _imports_the_engine(ast.parse(source), "htco_application.access.other"), source
    assert not _imports_the_engine(
        ast.parse("from htco_application.access.policy import decide"), "htco_application.x"
    )
