"""Claude Code project hooks (slice 1.6), wired in `.claude/settings.json`.

- `format` (PostToolUse on Edit and Write): formats the edited Python file and applies safe
  lint fixes, so agent edits never fail on style.
- `stop` (Stop): when Python or configuration files changed, runs `just check-fast` and refuses
  to let the agent finish while it fails (exit code 2; the reason goes to the agent). If it still
  fails after one more attempt, the agent may stop, but the user sees a warning: never silently.

Hook events arrive as JSON on stdin; see https://code.claude.com/docs/en/hooks.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

_PYTHON_SUFFIXES = {".py", ".pyi"}
# Changes to these make `just check-fast` worth running at the end of a turn.
_CHECKED_SUFFIXES = {*_PYTHON_SUFFIXES, ".toml"}
_CHECKED_NAMES = {"uv.lock", "justfile"}
_CHECK_COMMAND = ("uv", "run", "just", "check-fast")
_OUTPUT_TAIL_LINES = 60


@dataclass(frozen=True)
class Completed:
    returncode: int
    output: str


@dataclass(frozen=True)
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], Completed]


@dataclass(frozen=True)
class HookIO:
    """Everything a hook touches outside its arguments, injected so tests need no processes."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    project_dir: Path
    run: Runner
    git_status: Callable[[], str]


def format_edited_file(event: Mapping[str, Any], *, project_dir: Path, run: Runner) -> HookResult:
    """Format a Python file inside the project after an agent edited it. Never blocks."""
    tool_input = event.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, Mapping) else None
    if not isinstance(file_path, str):
        return HookResult(0)
    path = Path(file_path)
    if path.suffix not in _PYTHON_SUFFIXES:
        return HookResult(0)
    if not path.resolve().is_relative_to(project_dir.resolve()):
        return HookResult(0)
    run(["uv", "run", "ruff", "format", "--quiet", str(path)])
    run(["uv", "run", "ruff", "check", "--fix", "--quiet", "--exit-zero", str(path)])
    return HookResult(0)


def _is_checked(path: str) -> bool:
    candidate = Path(path)
    return candidate.suffix in _CHECKED_SUFFIXES or candidate.name in _CHECKED_NAMES


def stop_unless_checks_pass(
    event: Mapping[str, Any], *, changed: Sequence[str], run: Runner
) -> HookResult:
    """Block the end of the turn while `just check-fast` fails on changed code."""
    if not any(_is_checked(path) for path in changed):
        return HookResult(0)
    result = run(_CHECK_COMMAND)
    if result.returncode == 0:
        return HookResult(0)
    if event.get("stop_hook_active"):
        warning = (
            "`just check-fast` is still failing: the turn ended with failing checks. "
            "Run `uv run just check-fast` and fix them before committing."
        )
        return HookResult(0, stdout=json.dumps({"systemMessage": warning}))
    tail = "\n".join(result.output.splitlines()[-_OUTPUT_TAIL_LINES:])
    return HookResult(
        2,
        stderr=(
            "`just check-fast` failed. Fix the problems below before finishing "
            f"(never by weakening a test, ER-TEST-02):\n{tail}\n"
        ),
    )


def changed_paths(porcelain: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -z`, including both sides of a rename."""
    entries = [entry for entry in porcelain.split("\0") if entry]
    paths: list[str] = []
    index = 0
    while index < len(entries):
        status, path = entries[index][:2], entries[index][3:]
        paths.append(path)
        if "R" in status or "C" in status:
            index += 1
            paths.append(entries[index])
        index += 1
    return paths


def _run(args: Sequence[str], *, cwd: Path) -> Completed:
    result = subprocess.run(  # noqa: S603 - fixed commands, no user input
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    return Completed(result.returncode, result.stdout + result.stderr)


def _git_status(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def main(argv: list[str], io: HookIO) -> int:
    try:
        event = json.loads(io.stdin.read() or "{}")
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}

    if argv == ["format"]:
        result = format_edited_file(event, project_dir=io.project_dir, run=io.run)
    elif argv == ["stop"]:
        changed = changed_paths(io.git_status())
        result = stop_unless_checks_pass(event, changed=changed, run=io.run)
    else:
        io.stderr.write("usage: python -m tools.claude_hooks {format|stop}\n")
        return 1
    io.stdout.write(result.stdout)
    io.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    hook_io = HookIO(
        sys.stdin,
        sys.stdout,
        sys.stderr,
        project,
        lambda args: _run(args, cwd=project),
        lambda: _git_status(project),
    )
    raise SystemExit(main(sys.argv[1:], hook_io))
