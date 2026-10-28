"""Architecture guard: no automatic W3 Etsy write before Postgres approvals (audit 4.3, 11.2).

`EtsyApiChannel` sends Etsy writes only when a composition root hands it `writes=`. Today no root
does, and the worker's gateway approves through `MemoryApprovalService`: a write would wait for
an approval nobody can give, which is safe only because `writes` is not wired. The order that
must hold is: the Postgres approval service (Phase 7) and the owner's step-up route (6.6 part B)
first, then any wiring of `writes=`.

So every `EtsyApiChannel(..., writes=...)` in a composition root must either

- build, in the same function, a non-memory approval service (a `Postgres...Approval...` class)
  and no `MemoryApprovalService`; or
- be an owner-triggered site in the API, named in `OWNER_TRIGGERED_WRITE_SITES` with its reason
  (empty until 6.6 part B). The worker and the browser worker run automations, so they can never
  be on that list.

Any other use of `EtsyApiChannel` in a composition root (an alias, `**kwargs`) fails too, since
this guard could not see what it wires.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# The composition roots (the top layer of the dependency rule in pyproject.toml).
COMPOSITION_ROOTS = {
    "apps/api/src/htco_api": "api",
    "workers/runner/src/htco_worker": "worker",
    "workers/browser/src/htco_browser_worker": "browser-worker",
}
# Roots whose actions are owner-triggered requests; the others run automations.
OWNER_TRIGGERED_ROOTS = frozenset({"api"})
CHANNEL = "EtsyApiChannel"
MEMORY_APPROVALS = "MemoryApprovalService"
DURABLE_APPROVALS = re.compile(r"^Postgres\w*Approval\w*$")

# (path, enclosing function) -> why the owner triggers every write this site sends. Adding an
# entry is an architecture review: the route must enforce W3 (owner step-up), the flag and the
# kill switch, and no automation may reach this gateway.
OWNER_TRIGGERED_WRITE_SITES: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Site:
    path: str
    root: str
    function: str
    line: int
    wires_writes: bool
    opaque: bool
    durable_approvals: bool
    memory_approvals: bool


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _writes_wired(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "writes":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    return len(call.args) > 2  # a third positional argument would be `writes`


def sites_in(source: str, *, path: str, root: str) -> list[Site]:
    """Every use of `EtsyApiChannel` in one module, with what its enclosing function builds."""
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[Site] = []
    by_scope: dict[ast.AST, list[ast.AST]] = {}
    for node in ast.walk(tree):
        by_scope.setdefault(_scope_of(node, tree, parents), []).append(node)
    for scope, own in by_scope.items():
        calls = [n for n in own if isinstance(n, ast.Call)]
        names = {_called_name(c) for c in calls}
        durable = any(n is not None and DURABLE_APPROVALS.match(n) for n in names)
        memory = MEMORY_APPROVALS in names
        channel_calls = [c for c in calls if _called_name(c) == CHANNEL]
        callee_ids = {id(c.func) for c in channel_calls}
        function = getattr(scope, "name", "<module>")
        for call in channel_calls:
            opaque = any(k.arg is None for k in call.keywords) or any(
                isinstance(a, ast.Starred) for a in call.args
            )
            found.append(
                Site(path, root, function, call.lineno, _writes_wired(call) or opaque, opaque,
                     durable, memory)
            )  # fmt: skip
        for node in own:
            referenced = (isinstance(node, ast.Name) and node.id == CHANNEL) or (
                isinstance(node, ast.Attribute) and node.attr == CHANNEL
            )
            if referenced and id(node) not in callee_ids:
                found.append(Site(path, root, function, node.lineno, True, True, durable, memory))
    return found


def _scope_of(node: ast.AST, tree: ast.Module, parents: dict[int, ast.AST]) -> ast.AST:
    """The innermost function holding `node` (the module at top level)."""
    parent = parents.get(id(node))
    while parent is not None and not isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
        parent = parents.get(id(parent))
    return tree if parent is None else parent


def _all_sites() -> list[Site]:
    sites: list[Site] = []
    for directory, root in COMPOSITION_ROOTS.items():
        for file in sorted((REPO_ROOT / directory).rglob("*.py")):
            path = file.relative_to(REPO_ROOT).as_posix()
            sites += sites_in(file.read_text(encoding="utf-8"), path=path, root=root)
    return sites


def violations(sites: list[Site]) -> list[str]:
    found: list[str] = []
    for site in sites:
        where = f"{site.path}:{site.line} ({site.function})"
        if site.opaque:
            found.append(f"{where}: {CHANNEL} used in a way this guard cannot read")
            continue
        if not site.wires_writes:
            continue
        if site.durable_approvals and not site.memory_approvals:
            continue
        if (
            site.root in OWNER_TRIGGERED_ROOTS
            and (site.path, site.function) in OWNER_TRIGGERED_WRITE_SITES
        ):
            continue
        found.append(f"{where}: wires Etsy writes without a Postgres approval service (audit 4.3)")
    return found


def test_composition_roots__wire_no_automatic_w3_write_before_postgres_approvals() -> None:
    sites = _all_sites()
    # The guard looks at the real wiring: the worker builds the channel today (reads only).
    assert any(s.root == "worker" and not s.wires_writes for s in sites)
    assert violations(sites) == []


def test_owner_triggered_sites__are_api_sites_with_a_reason() -> None:
    for (path, function), reason in OWNER_TRIGGERED_WRITE_SITES.items():
        assert path.startswith("apps/api/src/htco_api/"), path
        assert function, path
        assert reason.strip(), (path, function)


def _check(source: str, root: str = "worker") -> list[str]:
    return violations(sites_in(source, path="x.py", root=root))


def test_guard__catches_writes_with_memory_approvals() -> None:
    source = """
def gateway(adapter, facts, writes):
    approvals = GatewayApprovals(MemoryApprovalService(clock=now), clock=now)
    return EtsyApiChannel(adapter, facts, writes=writes)
"""
    assert len(_check(source)) == 1


def test_guard__catches_writes_without_any_approval_service() -> None:
    assert len(_check("def g(a, f, w):\n    return EtsyApiChannel(a, f, writes=w)\n")) == 1


def test_guard__catches_an_alias_or_unpacked_arguments() -> None:
    assert len(_check("channel = EtsyApiChannel\n")) == 1
    assert len(_check("def g(a, f, k):\n    return EtsyApiChannel(a, f, **k)\n")) == 1


def test_guard__allows_reads_only_and_durable_approvals() -> None:
    assert _check("def g(a, f):\n    return EtsyApiChannel(a, f)\n") == []
    assert _check("def g(a, f):\n    return EtsyApiChannel(a, f, writes=None)\n") == []
    durable = """
def gateway(database, adapter, facts, writes):
    approvals = GatewayApprovals(PostgresApprovalService(database), clock=now)
    return EtsyApiChannel(adapter, facts, writes=writes)
"""
    assert _check(durable) == []


def test_guard__an_owner_triggered_site_is_allowed_only_in_the_api() -> None:
    source = "def owner_gateway(a, f, w):\n    return EtsyApiChannel(a, f, writes=w)\n"
    OWNER_TRIGGERED_WRITE_SITES[("x.py", "owner_gateway")] = "test"
    try:
        assert _check(source, root="api") == []
        assert len(_check(source, root="worker")) == 1
    finally:
        del OWNER_TRIGGERED_WRITE_SITES[("x.py", "owner_gateway")]
