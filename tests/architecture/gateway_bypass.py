"""Find where the source calls the Etsy adapter, for the gateway-bypass contract (audit P1-1).

The adapter's public coroutines are its Etsy operations. A reference to one of those names
anywhere else (a call, an alias, a callback, `partial`, `getattr` with that name) either goes
through the Action Gateway or bypasses it; the contract keeps a documented list of each site.
A `getattr` whose name is not written in the source fails closed (except on `self`, an object's
own fields).

A function of the adapter's module sends a write when it hands the choke point (`_request`, then
`_send`) a write flag that is not the literal `False` (`allow_write=…`, `write=…`, or `**` keyword
arguments), or reaches such a function through `self.` calls or the module's own helpers, private
ones included. A write may be referenced only from the gateway's API channel.

The scan is static and goes by name: it cannot tell a call through `GatewayReceipts` from one on
the raw adapter (whoever adds a gateway row checks the object), and dispatch beyond these forms
(`globals()`, `operator.attrgetter`, a name built at run time and passed to `getattr` on `self`) is
out of its reach.
"""

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ADAPTER_CLASS = "EtsyAdapter"
ADAPTER_MODULE = "htco_integrations.etsy"
# What a `getattr` with a name the source does not spell is reported as.
DYNAMIC_GETATTR = "getattr(?)"
# Any Etsy host named in source: etsy.com and every *.etsy.com, with what follows it up to a quote,
# a space or a closing parenthesis (an f-string's `{host}.etsy.com` shows as `.etsy.com`).
_ETSY_URL = re.compile(r"(?<![a-z0-9-])\.?(?:[a-z0-9-]+\.)*etsy\.com\b[^\s\"'`)]*", re.IGNORECASE)
# Every request passes these two; what their callers hand them decides a write (the contract
# pins that only `_request` calls `_send`).
CHOKE_POINT = frozenset({"_request", "_send"})
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


def _not_false(value: ast.expr) -> bool:
    return not (isinstance(value, ast.Constant) and value.value is False)


def _sends_write(function: _Function) -> bool:
    """It hands the choke point a write flag that is not the literal `False`, or it reaches a
    name it does not spell (`getattr` with a computed name fails closed)."""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if _dynamic_getattr(node):
            return True
        name = _called_name(node)
        for keyword in node.keywords:
            if keyword.arg == "allow_write" and _not_false(keyword.value):
                return True
            if keyword.arg is None and name in CHOKE_POINT:  # **options: any flag at all
                return True
            if keyword.arg == "write" and name == "_send" and _not_false(keyword.value):
                return True
    return False


def _callees(function: _Function, methods: set[str], helpers: set[str]) -> set[str]:
    """The adapter's methods (`self.x(…)`) and the module's functions (`x(…)`) it calls."""
    called: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        literal = _getattr_name(target) if isinstance(target, ast.Call) else None
        if literal is not None and literal in methods:  # getattr(self, "x")(...)
            called.add(literal)
        elif (
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
    # A method shadows a helper of the same name; the choke point is judged by its callers.
    functions = {
        name: node for name, node in (helpers | methods).items() if name not in CHOKE_POINT
    }
    graph = {name: _callees(node, set(methods), set(helpers)) for name, node in functions.items()}
    writes = {name for name, node in functions.items() if _sends_write(node)}
    while True:
        more = {name for name, callees in graph.items() if callees & writes} - writes
        if not more:
            return frozenset(writes)
        writes |= more


def _walk(node: ast.AST, scope: tuple[str, ...]) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, *_FUNCTIONS)):
            yield from _walk(child, (*scope, child.name))
            continue
        yield child, scope
        yield from _walk(child, scope)


def _is_getattr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2  # getattr(object, name)
    )


def _getattr_name(node: ast.Call) -> str | None:
    """The name a `getattr` spells in the source, or None."""
    if not _is_getattr(node):
        return None
    name = node.args[1]
    return name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None


def _dynamic_getattr(node: ast.Call) -> bool:
    return _is_getattr(node) and _getattr_name(node) is None


def _called_name(call: ast.Call) -> str | None:
    """The name a call reaches: `x.name(…)`, `name(…)` or `getattr(x, "name")(…)`."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Call):
        return _getattr_name(call.func)
    return None


def etsy_urls(text: str) -> list[str]:
    """Every Etsy host named in `text`, lower-cased, with its path, in order."""
    return [match.group(0).lower() for match in _ETSY_URL.finditer(text)]


def channel_runs(tree: ast.AST) -> bool:
    """`tree` runs a gateway channel: it calls `.execute(...)` with a channel `binding`."""
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(
            (isinstance(a, ast.Name) and a.id == "binding")
            or (isinstance(a, ast.Attribute) and a.attr == "binding")
            for a in arguments
        ):
            return True
    return False


def _referenced(node: ast.AST, methods: frozenset[str]) -> str | None:
    """The operation `node` refers to, `DYNAMIC_GETATTR` for a name the source does not spell,
    or None."""
    if isinstance(node, ast.Attribute) and node.attr in methods:
        return node.attr
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in methods:
        return node.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2  # getattr(object, name)
    ):
        target, name = node.args[0], node.args[1]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            return name.value if name.value in methods else None
        if not (isinstance(target, ast.Name) and target.id == "self"):
            return DYNAMIC_GETATTR
    return None


def calls_of(roots: list[Path], methods: frozenset[str], *, skip: Path) -> list[Call]:
    """Every reference to one of `methods` under `roots` (called or not, `getattr` included),
    with the function it is in, except in `skip` (the adapter itself)."""
    calls: list[Call] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == skip.resolve():
                continue
            calls.extend(
                Call(path, getattr(node, "lineno", 0), name, ".".join(scope) or "<module>")
                for node, scope in _walk(_parse(path), ())
                if (name := _referenced(node, methods)) is not None
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
