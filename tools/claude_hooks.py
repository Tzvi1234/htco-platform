"""Claude Code project hooks (slice 1.6), wired in `.claude/settings.json`.

- `format` (PostToolUse on Edit and Write): formats the edited Python file and applies safe
  lint fixes, so agent edits never fail on style.
- `turn-start` (UserPromptSubmit): records the commit the turn starts from.
- `stop` (Stop): when any file changed during the turn, committed or not, runs
  `just check-fast` and refuses to let the agent finish while it fails (exit code 2; the reason
  goes to the agent). If it still fails after one more attempt, the agent may stop, but the user
  sees a warning: never silently.

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
_CHECK_COMMAND = ("uv", "run", "just", "check-fast")
_STATUS_COMMAND = ("status", "--porcelain=v1", "-z", "--untracked-files=all")
_OUTPUT_TAIL_LINES = 60
# Stands in for "something changed, but git cannot say what", so the checks still run.
_UNKNOWN_CHANGE = "<history rewritten during the turn>"


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
# Runs a git command; returns its standard output, or None when the command failed.
Git = Callable[[Sequence[str]], str | None]


@dataclass(frozen=True)
class HookIO:
    """Everything a hook touches outside its arguments, injected so tests need no processes."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    project_dir: Path
    run: Runner
    git: Git
    turn_state: Path


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


def stop_unless_checks_pass(
    event: Mapping[str, Any], *, changed: Sequence[str], run: Runner
) -> HookResult:
    """Block the end of the turn while `just check-fast` fails after any change.

    Every file counts: tests read code, configuration, the Dockerfile, the contract and some
    documents (the permission matrix) alike.
    """
    if not changed:
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


def record_turn_start(git: Git, state: Path) -> None:
    """Remember the commit the turn starts from, so work committed during it is still seen."""
    head = (git(["rev-parse", "HEAD"]) or "").strip()
    state.write_text(head, encoding="utf-8")


def paths_changed_in_turn(git: Git, state: Path) -> list[str]:
    """Uncommitted changes plus every file committed since the turn started."""
    paths = changed_paths(git(_STATUS_COMMAND) or "")
    start = state.read_text(encoding="utf-8").strip() if state.is_file() else ""
    if start:
        committed = git(["diff", "--name-only", "-z", start, "HEAD"])
        if committed is None:
            paths.append(_UNKNOWN_CHANGE)
        else:
            paths += [path for path in committed.split("\0") if path]
    return list(dict.fromkeys(paths))


def _run(args: Sequence[str], *, cwd: Path) -> Completed:
    result = subprocess.run(  # noqa: S603 - fixed commands, no user input
        list(args), cwd=cwd, capture_output=True, text=True, check=False
    )
    return Completed(result.returncode, result.stdout + result.stderr)


def _git(args: Sequence[str], *, cwd: Path) -> str | None:
    result = subprocess.run(  # noqa: S603 - fixed git commands, no user input
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def main(argv: list[str], io: HookIO) -> int:
    try:
        event = json.loads(io.stdin.read() or "{}")
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}

    if argv == ["format"]:
        result = format_edited_file(event, project_dir=io.project_dir, run=io.run)
    elif argv == ["turn-start"]:
        record_turn_start(io.git, io.turn_state)
        result = HookResult(0)
    elif argv == ["stop"]:
        changed = paths_changed_in_turn(io.git, io.turn_state)
        result = stop_unless_checks_pass(event, changed=changed, run=io.run)
    else:
        io.stderr.write("usage: python -m tools.claude_hooks {format|turn-start|stop}\n")
        return 1
    io.stdout.write(result.stdout)
    io.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())

    def project_git(args: Sequence[str]) -> str | None:
        return _git(args, cwd=project)

    # Inside the git directory: per worktree, never committed.
    state_path = (project_git(["rev-parse", "--git-path", "htco-turn-start"]) or "").strip()
    hook_io = HookIO(
        sys.stdin,
        sys.stdout,
        sys.stderr,
        project,
        lambda args: _run(args, cwd=project),
        project_git,
        project / (state_path or ".git/htco-turn-start"),
    )
    raise SystemExit(main(sys.argv[1:], hook_io))
