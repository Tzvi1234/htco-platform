"""Architecture contract: the shared kernel (AUD-02 step 1, AUD-15).

Shared types and small ports live once, in `htco_application.kernel`. A feature module that
declares its own copy fails here, so the copies cannot drift again.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.architecture.purity import find_forbidden_imports

REPO_ROOT = Path(__file__).resolve().parents[2]
APPLICATION_SOURCE = REPO_ROOT / "packages" / "application" / "src" / "htco_application"
KERNEL_SOURCE = APPLICATION_SOURCE / "kernel"

# The names the kernel owns: any other class, alias, function or constant with one of these
# names inside `htco_application` is a second copy.
KERNEL_NAMES = frozenset(
    {
        "ConnectedShops",
        "DomainEvent",
        "JobError",
        "JsonValue",
        "OutboxMessage",
        "Offload",
        "Page",
        "ShopOwners",
        "TenantShop",
        "describe_error",
        "read_event",
        "run_inline",
    }
)
EVENTS_DESTINATION_VALUE = "events.publish"
# An outbox message built by hand for the events destination skips the envelope (AUD-01).
UNPUBLISHED_EVENT = "OutboxMessage(destination=events)"

# Copies inside areas another teammate has locked on the board (#95). Each one moves in its
# own follow-up PR once the lock clears; the entry goes with it, and nothing may be added.
PENDING_COPIES = frozenset(
    {
        ("execution/service.py", "ShopOwners"),
        ("execution/service.py", EVENTS_DESTINATION_VALUE),
        ("execution/service.py", UNPUBLISHED_EVENT),
    }
)


def _definitions(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Every module-level or nested class, function, type alias and assigned name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.lineno, node.name
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            yield node.lineno, node.name.id
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield node.lineno, target.id


def _inline_offloads(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """`async def anything(work): return work()`: an inline offload under another name."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or len(node.args.args) != 1:
            continue
        parameter = node.args.args[0].arg
        match node.body:
            case [ast.Return(value=ast.Call(func=ast.Name(id=called), args=[], keywords=[]))]:
                if called == parameter:
                    yield node.lineno, "run_inline"
            case _:
                pass


def _destination_literals(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == EVENTS_DESTINATION_VALUE:
            yield node.lineno, EVENTS_DESTINATION_VALUE


def _unpublished_events(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """`OutboxMessage(destination=EVENTS_DESTINATION, ...)`: an event built without `publish`."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "OutboxMessage":
            continue
        for keyword in node.keywords:
            value = keyword.value
            if keyword.arg == "destination" and (
                (isinstance(value, ast.Name) and value.id.startswith("EVENT"))
                or (isinstance(value, ast.Constant) and value.value == EVENTS_DESTINATION_VALUE)
            ):
                yield node.lineno, UNPUBLISHED_EVENT


def find_kernel_copies(source_dir: Path) -> list[str]:
    copies = []
    for path in sorted(source_dir.rglob("*.py")):
        relative = path.relative_to(source_dir).as_posix()
        if relative.startswith("kernel/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = [(line, name) for line, name in _definitions(tree) if name in KERNEL_NAMES]
        found += list(_inline_offloads(tree))
        found += list(_destination_literals(tree))
        found += list(_unpublished_events(tree))
        copies += [
            f"{relative}:{line} defines {name!r}"
            for line, name in sorted(set(found))
            if (relative, name) not in PENDING_COPIES
        ]
    return copies


@pytest.mark.ac("AC-140.1")
def test_application__shared_types_and_ports__are_defined_only_in_the_kernel() -> None:
    copies = find_kernel_copies(APPLICATION_SOURCE)

    assert copies == [], "Use htco_application.kernel instead:\n  " + "\n  ".join(copies)


@pytest.mark.ac("AC-140.1")
def test_application__pending_copies__still_exist() -> None:
    # A pending entry whose copy is gone must be deleted, so the list only shrinks.
    stale = []
    for relative, name in PENDING_COPIES:
        tree = ast.parse((APPLICATION_SOURCE / relative).read_text(encoding="utf-8"))
        names = {n for _, n in _definitions(tree)} | {n for _, n in _inline_offloads(tree)}
        names |= {n for _, n in _destination_literals(tree)}
        names |= {n for _, n in _unpublished_events(tree)}
        if name not in names:
            stale.append((relative, name))

    assert stale == []


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("from typing import Protocol\nclass ShopOwners(Protocol): ...\n", "ShopOwners"),
        ("type Offload = object\n", "Offload"),
        ("async def _inline(work):\n    return work()\n", "run_inline"),
        ("class TenantShop: ...\n", "TenantShop"),
        ("type JsonValue = str\n", "JsonValue"),
        ("def describe_error(error): ...\n", "describe_error"),
        ("EVENTS = 'events.publish'\n", EVENTS_DESTINATION_VALUE),
        ("m = OutboxMessage(destination=EVENTS_DESTINATION)\n", UNPUBLISHED_EVENT),
    ],
)
def test_find_kernel_copies__a_second_copy__is_reported(
    tmp_path: Path, source: str, name: str
) -> None:
    (tmp_path / "feature.py").write_text(source, encoding="utf-8")

    copies = find_kernel_copies(tmp_path)

    assert len(copies) == 1
    assert repr(name) in copies[0]


def test_find_kernel_copies__the_kernel_itself__is_allowed(tmp_path: Path) -> None:
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "types.py").write_text("class TenantShop: ...\n", encoding="utf-8")
    (tmp_path / "feature.py").write_text(
        "from htco_application.kernel import TenantShop\n", encoding="utf-8"
    )

    assert find_kernel_copies(tmp_path) == []


@pytest.mark.ac("AC-140.5")
def test_kernel__imports__only_the_standard_library_the_domain_and_itself() -> None:
    violations = find_forbidden_imports(KERNEL_SOURCE, allowed=frozenset({"htco_domain"}))
    feature_imports = [
        (v.path.name, v.line, v.module)
        for v in violations
        if not v.module.startswith("htco_application.kernel")
    ]
    application_imports = [
        (path.name, node.lineno, node.module)
        for path in sorted(KERNEL_SOURCE.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("htco_application")
        and not node.module.startswith("htco_application.kernel")
    ]

    assert feature_imports == []
    assert application_imports == []
