"""Find imports that break a layer's allow-list (standard library plus named packages)."""

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ForbiddenImport:
    path: Path
    line: int
    module: str


def find_forbidden_imports(source_dir: Path, *, allowed: frozenset[str]) -> list[ForbiddenImport]:
    """Return every absolute import under `source_dir` outside the standard library and `allowed`.

    Every import statement counts, including imports inside functions and `TYPE_CHECKING`
    blocks: a pure layer must not even name an outer dependency. Relative imports stay inside
    the package and are always allowed.
    """
    violations = []
    for path in sorted(source_dir.rglob("*.py")):
        for line, module in _absolute_imports(path.read_text(encoding="utf-8")):
            top_level = module.partition(".")[0]
            if top_level not in sys.stdlib_module_names and top_level not in allowed:
                violations.append(ForbiddenImport(path=path, line=line, module=module))
    return violations


def _absolute_imports(source: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            yield node.lineno, node.module
