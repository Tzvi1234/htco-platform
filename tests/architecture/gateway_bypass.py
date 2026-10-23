"""Find where the source calls the Etsy adapter, for the gateway-bypass contract (audit P1-1).

The adapter's public coroutines are its Etsy operations. A call of one of those names anywhere
else is a call that either goes through the Action Gateway or bypasses it; the contract keeps a
documented list of each. A method that passes `allow_write=True` sends a write, and may be called
only from the gateway's API channel.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

ADAPTER_CLASS = "EtsyAdapter"
ADAPTER_MODULE = "htco_integrations.etsy"


@dataclass(frozen=True, order=True)
class Call:
    path: Path
    line: int
    method: str


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _adapter_methods(adapter_source: Path) -> list[ast.AsyncFunctionDef]:
    tree = _parse(adapter_source)
    adapter = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLASS
    )
    return [
        node
        for node in adapter.body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    ]


def _sends_a_write(method: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.keyword)
        and node.arg == "allow_write"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        for node in ast.walk(method)
    )


def etsy_operations(adapter_source: Path) -> frozenset[str]:
    """The adapter's public Etsy operations: its public coroutines."""
    return frozenset(method.name for method in _adapter_methods(adapter_source))


def etsy_writes(adapter_source: Path) -> frozenset[str]:
    """The operations that send a write to Etsy (`allow_write=True` in their body)."""
    return frozenset(
        method.name for method in _adapter_methods(adapter_source) if _sends_a_write(method)
    )


def calls_of(roots: list[Path], methods: frozenset[str], *, skip: Path) -> list[Call]:
    """Every `<anything>.<method>(…)` call of one of `methods` under `roots`, except in `skip`
    (the adapter itself)."""
    calls: list[Call] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == skip.resolve():
                continue
            calls.extend(
                Call(path, node.lineno, node.func.attr)
                for node in ast.walk(_parse(path))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            )
    return sorted(calls)


def importers_of(roots: list[Path], module: str) -> list[Path]:
    """Every source file under `roots` that imports `module` (or a name from it)."""
    parent, _, leaf = module.rpartition(".")
    found: list[Path] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.Import) and any(
                    alias.name == module or alias.name.startswith(module + ".")
                    for alias in node.names
                ):
                    found.append(path)
                    break
                if isinstance(node, ast.ImportFrom) and (
                    node.module == module
                    or (node.module or "").startswith(module + ".")
                    or (node.module == parent and any(alias.name == leaf for alias in node.names))
                ):
                    found.append(path)
                    break
    return found
