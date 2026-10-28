"""Architecture contract: Etsy writes are wired only for the Owner's own press (ADR-0022, 6.8).

W3 approvals live in memory until Phase 7's Postgres approvals, so no automation or agent may be
able to run an Etsy write yet. The API's owner-writes composition is the only place that gives
the gateway's API channel its `writes=`: there, only the Owner's press after a step-up runs, and
only once the owner names the write's flag. A worker (or any other root) wiring `writes=` fails
here until Postgres approvals exist and this contract is changed on purpose.
"""

import ast
from pathlib import Path

from htco_application.owner_writes import WriteSwitches

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = [
    *REPO_ROOT.glob("packages/*/src"),
    *REPO_ROOT.glob("apps/*/src"),
    *REPO_ROOT.glob("workers/*/src"),
]
# (module, enclosing function): the one wiring of the writes.
ALLOWED = {("apps/api/src/htco_api/main.py", "owner_write_services")}


def _wirings() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Call)
                        and getattr(node.func, "id", getattr(node.func, "attr", None))
                        == "EtsyApiChannel"
                        and any(k.arg == "writes" for k in node.keywords)
                    ):
                        relative = path.relative_to(REPO_ROOT).as_posix()
                        found.add((relative, function.name))
    return found


def test_only_the_owners_press_wires_the_etsy_writes() -> None:
    assert _wirings() == ALLOWED


def test_the_worker_never_wires_the_etsy_writes() -> None:
    """No automatic W3 write before Postgres approvals: the worker's channel stays read-only."""
    assert not any(module.startswith("workers/") for module, _ in _wirings())


def test_every_write_is_off_until_the_owner_names_its_flag() -> None:
    switches = WriteSwitches()
    assert not switches.is_on("etsy.shipments.create")
    assert not switches.is_on("etsy.shop.update")
    halted = WriteSwitches(frozenset({"etsy.shop.update"}), halted=lambda: True)
    assert halted.flag_on("etsy.shop.update")
    assert not halted.is_on("etsy.shop.update")
