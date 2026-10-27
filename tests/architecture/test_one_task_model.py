"""One task model (AC-7.1.4, AUD-03): `htco_domain.tasks.TaskState` is the only task state
machine, and `app.tasks` with `app.task_events` the only task tables.

A second task state enum fails here: a `StrEnum` named `*TaskState` or `*TaskStatus`, or any
`StrEnum` with a `completed` member (the task's own word). `KNOWN` lists the one that exists
today, with the follow-up that removes it, so the list can only shrink. `NOT_TASKS` lists enums
that share the word but are no task's state.
"""

import ast
import re
from pathlib import Path
from typing import TypeGuard

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"
MIGRATIONS = REPO_ROOT / "supabase" / "migrations"
THE_TASK_STATE = ("domain/src/htco_domain/tasks.py", "TaskState")

# Task state enums that predate the one model, each with the follow-up that removes it.
KNOWN = {
    ("application/src/htco_application/execution/channels.py", "HumanTaskState"): "#198",
}
# Enums with a `completed` member that are not a task's state.
NOT_TASKS = {
    # An agent session's runtime state: a finished conversation never completes its task
    # (FR-TASK-02); the task completes only on evidence.
    ("application/src/htco_application/agents/runtime.py", "SessionStatus"),
}
TASK_TABLES = {"app.tasks", "app.task_events"}

_TASK_NAME = re.compile(r"Task(State|Status)$")
_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_]+\.[a-z0-9_]+)", re.IGNORECASE
)


def _is_enum(node: ast.ClassDef) -> bool:
    names = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
    return bool(names & {"StrEnum", "Enum", "IntEnum"})


def _values(node: ast.ClassDef) -> set[str]:
    return {
        statement.value.value
        for statement in node.body
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    }


def _is_task_like(node: ast.AST) -> TypeGuard[ast.ClassDef]:
    return (
        isinstance(node, ast.ClassDef)
        and _is_enum(node)
        and bool(_TASK_NAME.search(node.name) or "completed" in _values(node))
    )


def task_like_enums() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(PACKAGES.glob("*/src/**/*.py")):
        relative = path.relative_to(PACKAGES).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if _is_task_like(node):
                found.add((relative, node.name))
    return found


def task_tables() -> set[str]:
    return {
        match.group(1).lower()
        for path in sorted(MIGRATIONS.glob("*.sql"))
        for match in _CREATE_TABLE.finditer(path.read_text(encoding="utf-8"))
        if "task" in match.group(1).lower()
    }


@pytest.mark.ac("AC-7.1.4")
def test_one_task_state_machine__no_second_task_state_enum() -> None:
    unexpected = task_like_enums() - {THE_TASK_STATE} - set(KNOWN) - NOT_TASKS

    assert not unexpected, (
        f"a second task state enum: {sorted(unexpected)}. Tasks of every kind are "
        "htco_domain.tasks.BusinessTask with TaskState (AC-7.1.4); a kind adds data, never states"
    )


@pytest.mark.ac("AC-7.1.4")
def test_one_task_state_machine__the_allowlist_only_shrinks() -> None:
    """An entry whose enum is gone must leave the lists (its follow-up is done)."""
    present = task_like_enums()

    assert THE_TASK_STATE in present
    assert set(KNOWN) <= present, f"remove from KNOWN: {sorted(set(KNOWN) - present)}"
    assert present >= NOT_TASKS, f"remove from NOT_TASKS: {sorted(NOT_TASKS - present)}"


@pytest.mark.ac("AC-7.1.4")
def test_one_task_model__no_second_task_table() -> None:
    assert task_tables() == TASK_TABLES


def test_the_guard__finds_what_it_must() -> None:
    """The detector itself: a renamed or `completed`-carrying enum is found, others are not."""
    source = (
        "from enum import StrEnum\n"
        "class RelayItemStatus(StrEnum):\n    OPEN = 'open'\n    COMPLETED = 'completed'\n"
        "class ShippingTaskState(StrEnum):\n    NEW = 'new'\n"
        "class Colour(StrEnum):\n    RED = 'red'\n"
    )
    names = {node.name for node in ast.walk(ast.parse(source)) if _is_task_like(node)}
    sql = "create table app.relay_tasks (id uuid);\ncreate table if not exists app.other (id int);"

    assert names == {"RelayItemStatus", "ShippingTaskState"}
    assert [m.group(1) for m in _CREATE_TABLE.finditer(sql)] == ["app.relay_tasks", "app.other"]
