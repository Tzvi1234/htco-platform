"""Find where the source calls the Etsy adapter, for the gateway-bypass contract (audit P1-1).

The adapter's public coroutines are its Etsy operations. A call of one of those names anywhere
else is a call that either goes through the Action Gateway or bypasses it; the contract keeps a
documented list of each call site. A function of the adapter's module that passes
`allow_write=True`, or reaches one that does (through `self.` calls or the module's own helpers,
private ones included), sends a write, and may be called only from the gateway's API channel.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ADAPTER_CLASS = "EtsyAdapter"
ADAPTER_MODULE = "htco_integrations.etsy"
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, order=True)
class Call:
    path: Path
    line: int
    method: str
    where: str  # the qualified function the call is in ("Class.method"), or "<module>"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _adapter(tree: ast.Module) -> ast.ClassDef:
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLASS
    )


def etsy_operations(adapter_source: Path) -> frozenset[str]:
    """The adapter's public Etsy operations: its public coroutines."""
    return frozenset(
        node.name
        for node in _adapter(_parse(adapter_source)).body
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
    )


def _sets_allow_write(function: _Function) -> bool:
    return any(
        isinstance(node, ast.keyword)
        and node.arg == "allow_write"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        for node in ast.walk(function)
    )


def _callees(function: _Function, methods: set[str], helpers: set[str]) -> set[str]:
    """The adapter's methods (`self.x(…)`) and the module's functions (`x(…)`) it calls."""
    called: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr in methods
        ):
            called.add(target.attr)
        elif isinstance(target, ast.Name) and target.id in helpers:
            called.add(target.id)
    return called


def etsy_writes(adapter_source: Path) -> frozenset[str]:
    """Every function of the adapter's module, private or public, that sends a write: it sets
    `allow_write=True` itself or reaches a function that does."""
    tree = _parse(adapter_source)
    methods = {n.name: n for n in _adapter(tree).body if isinstance(n, _FUNCTIONS)}
    helpers = {n.name: n for n in tree.body if isinstance(n, _FUNCTIONS)}
    functions = helpers | methods  # a method shadows a helper of the same name
    graph = {name: _callees(node, set(methods), set(helpers)) for name, node in functions.items()}
    writes = {name for name, node in functions.items() if _sets_allow_write(node)}
    while True:
        more = {name for name, callees in graph.items() if callees & writes} - writes
        if not more:
            return frozenset(writes)
        writes |= more


def _walk(node: ast.AST, scope: tuple[str, ...]) -> Iterator[tuple[ast.Call, tuple[str, ...]]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, *_FUNCTIONS)):
            yield from _walk(child, (*scope, child.name))
            continue
        if isinstance(child, ast.Call):
            yield child, scope
        yield from _walk(child, scope)


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def calls_of(roots: list[Path], methods: frozenset[str], *, skip: Path) -> list[Call]:
    """Every call of one of `methods` under `roots` (`x.method(…)` or `method(…)`), with the
    function it is in, except in `skip` (the adapter itself)."""
    calls: list[Call] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == skip.resolve():
                continue
            calls.extend(
                Call(path, call.lineno, name, ".".join(scope) or "<module>")
                for call, scope in _walk(_parse(path), ())
                if (name := _called_name(call)) is not None and name in methods
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
