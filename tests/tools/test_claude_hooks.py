"""Claude Code project hooks: format edited files, refuse to stop on failing checks (AC-1.6.1)."""

import io
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tools.claude_hooks import (
    Completed,
    HookIO,
    HookResult,
    changed_paths,
    format_edited_file,
    main,
    paths_changed_in_turn,
    record_turn_start,
    stop_unless_checks_pass,
)


class _Runner:
    def __init__(self, returncode: int = 0, output: str = "") -> None:
        self.calls: list[list[str]] = []
        self._result = Completed(returncode, output)

    def __call__(self, args: Sequence[str]) -> Completed:
        self.calls.append(list(args))
        return self._result


class _Git:
    """Answers git commands from a table; an unknown command fails like git would (None)."""

    def __init__(self, answers: dict[tuple[str, ...], str]) -> None:
        self._answers = answers

    def __call__(self, args: Sequence[str]) -> str | None:
        return self._answers.get(tuple(args))


STATUS = ("status", "--porcelain=v1", "-z", "--untracked-files=all")


def _edit_event(path: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
    }


# --- PostToolUse: format the edited file -----------------------------------------------------


def test_format__python_file_in_the_project__is_formatted_and_fixed(tmp_path: Path) -> None:
    module = tmp_path / "packages" / "domain" / "orders.py"
    run = _Runner()

    result = format_edited_file(_edit_event(module), project_dir=tmp_path, run=run)

    assert result == HookResult(0)
    assert run.calls == [
        ["uv", "run", "ruff", "format", "--quiet", str(module)],
        ["uv", "run", "ruff", "check", "--fix", "--quiet", "--exit-zero", str(module)],
    ]


@pytest.mark.parametrize("name", ["README.md", "pyproject.toml", "ci.yml", "notes.txt"])
def test_format__non_python_file__is_left_alone(tmp_path: Path, name: str) -> None:
    run = _Runner()

    result = format_edited_file(_edit_event(tmp_path / name), project_dir=tmp_path, run=run)

    assert result == HookResult(0)
    assert run.calls == []


def test_format__python_file_outside_the_project__is_left_alone(tmp_path: Path) -> None:
    run = _Runner()
    outside = tmp_path.parent / "elsewhere.py"

    format_edited_file(_edit_event(outside), project_dir=tmp_path, run=run)

    assert run.calls == []


def test_format__event_without_a_file_path__does_nothing(tmp_path: Path) -> None:
    run = _Runner()

    result = format_edited_file({"tool_name": "Edit"}, project_dir=tmp_path, run=run)

    assert result == HookResult(0)
    assert run.calls == []


# --- Stop: block while checks fail -----------------------------------------------------------


@pytest.mark.ac("AC-1.6.1")
def test_stop__checks_fail__blocks_and_tells_the_agent_why() -> None:
    run = _Runner(
        returncode=1, output="FAILED tests/api/test_health.py::test_healthz - assert 500 == 200"
    )

    result = stop_unless_checks_pass({}, changed=["apps/api/src/htco_api/health.py"], run=run)

    assert result.exit_code == 2
    assert "just check-fast" in result.stderr
    assert "FAILED tests/api/test_health.py::test_healthz" in result.stderr
    assert run.calls == [["uv", "run", "just", "check-fast"]]


def test_stop__checks_pass__lets_the_agent_finish() -> None:
    run = _Runner(returncode=0)

    result = stop_unless_checks_pass({}, changed=["tests/api/test_health.py"], run=run)

    assert result == HookResult(0)


def test_stop__nothing_changed__skips_the_checks() -> None:
    run = _Runner(returncode=1)

    result = stop_unless_checks_pass({}, changed=[], run=run)

    assert result == HookResult(0)
    assert run.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "justfile",
        "tools/x.py",
        "a/b.pyi",
        "Dockerfile",
        "packages/contracts/openapi.yaml",
        ".claude/settings.json",
        "docs/architecture/PERMISSION_MATRIX.md",
        ".github/workflows/ci.yml",
    ],
)
def test_stop__any_file_changed__runs_the_checks(path: str) -> None:
    """Tests read code, configuration, the contract and some documents alike."""
    run = _Runner(returncode=0)

    stop_unless_checks_pass({}, changed=[path], run=run)

    assert run.calls == [["uv", "run", "just", "check-fast"]]


@pytest.mark.ac("AC-1.6.1")
def test_stop__still_failing_after_one_retry__lets_go_but_warns_the_user() -> None:
    run = _Runner(returncode=1, output="1 failed")

    result = stop_unless_checks_pass({"stop_hook_active": True}, changed=["x.py"], run=run)

    assert result.exit_code == 0
    message = json.loads(result.stdout)["systemMessage"]
    assert "check-fast" in message
    assert "failing" in message


def test_stop__long_output__keeps_only_the_tail() -> None:
    output = "\n".join(f"line {number}" for number in range(500))
    run = _Runner(returncode=1, output=output)

    result = stop_unless_checks_pass({}, changed=["x.py"], run=run)

    assert "line 499" in result.stderr
    assert "line 100\n" not in result.stderr


# --- changed files ---------------------------------------------------------------------------


def test_changed_paths__porcelain_z_output__lists_every_path_including_renames() -> None:
    porcelain = (
        " M apps/api/src/htco_api/app.py\0?? tests/new test.py\0R  tools/new.py\0tools/old.py\0"
    )

    assert changed_paths(porcelain) == [
        "apps/api/src/htco_api/app.py",
        "tests/new test.py",
        "tools/new.py",
        "tools/old.py",
    ]


def test_changed_paths__clean_tree__is_empty() -> None:
    assert changed_paths("") == []


# --- what changed during the turn -------------------------------------------------------------


def test_record_turn_start__stores_the_head_commit(tmp_path: Path) -> None:
    state = tmp_path / "turn-base"

    record_turn_start(_Git({("rev-parse", "HEAD"): "abc123\n"}), state)

    assert state.read_text(encoding="utf-8") == "abc123"


@pytest.mark.ac("AC-1.6.1")
def test_paths_changed_in_turn__work_committed_during_the_turn__is_included(
    tmp_path: Path,
) -> None:
    state = tmp_path / "turn-base"
    state.write_text("abc123", encoding="utf-8")
    git = _Git({STATUS: "", ("diff", "--name-only", "-z", "abc123", "HEAD"): "tools/x.py\0"})

    assert paths_changed_in_turn(git, state) == ["tools/x.py"]


def test_paths_changed_in_turn__uncommitted_and_committed__are_both_listed(tmp_path: Path) -> None:
    state = tmp_path / "turn-base"
    state.write_text("abc123", encoding="utf-8")
    git = _Git(
        {STATUS: " M README.md\0", ("diff", "--name-only", "-z", "abc123", "HEAD"): "a.py\0"}
    )

    assert paths_changed_in_turn(git, state) == ["README.md", "a.py"]


def test_paths_changed_in_turn__no_recorded_start__uses_the_working_tree(tmp_path: Path) -> None:
    git = _Git({STATUS: " M tools/x.py\0"})

    assert paths_changed_in_turn(git, tmp_path / "missing") == ["tools/x.py"]


def test_paths_changed_in_turn__recorded_start_unreachable__assumes_a_change(
    tmp_path: Path,
) -> None:
    """A rewritten history must not hide the turn's work: the checks run."""
    state = tmp_path / "turn-base"
    state.write_text("gone", encoding="utf-8")

    assert paths_changed_in_turn(_Git({STATUS: ""}), state) != []


# --- command line ----------------------------------------------------------------------------


def test_main__invalid_json_on_stdin__is_treated_as_an_empty_event(tmp_path: Path) -> None:
    run = _Runner()
    stdout, stderr = io.StringIO(), io.StringIO()

    hooks = HookIO(io.StringIO("not json"), stdout, stderr, tmp_path, run, _Git({}), tmp_path / "s")

    exit_code = main(["format"], hooks)

    assert exit_code == 0
    assert run.calls == []


@pytest.mark.ac("AC-1.6.1")
def test_main__stop_with_failing_checks__exits_2_with_the_reason_on_stderr(tmp_path: Path) -> None:
    run = _Runner(returncode=1, output="E   AssertionError")
    stdout, stderr = io.StringIO(), io.StringIO()

    event = io.StringIO(json.dumps({"stop_hook_active": False}))
    git = _Git({STATUS: " M tools/x.py\0"})
    hooks = HookIO(event, stdout, stderr, tmp_path, run, git, tmp_path / "missing")

    exit_code = main(["stop"], hooks)

    assert exit_code == 2
    assert "AssertionError" in stderr.getvalue()


def test_main__turn_start__records_head(tmp_path: Path) -> None:
    state = tmp_path / "turn-base"
    git = _Git({("rev-parse", "HEAD"): "def456\n"})
    hooks = HookIO(io.StringIO("{}"), io.StringIO(), io.StringIO(), tmp_path, _Runner(), git, state)

    assert main(["turn-start"], hooks) == 0
    assert state.read_text(encoding="utf-8") == "def456"


# --- the settings that wire the hooks --------------------------------------------------------

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"


def _hook_commands(event: str) -> list[tuple[str | None, str]]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return [
        (group.get("matcher"), hook["command"])
        for group in settings["hooks"][event]
        for hook in group["hooks"]
    ]


def test_settings__edits_trigger_the_format_hook() -> None:
    [(matcher, command)] = _hook_commands("PostToolUse")

    assert matcher is not None
    assert {"Edit", "Write"} <= set(matcher.split("|"))
    assert command.endswith("python -m tools.claude_hooks format")


@pytest.mark.ac("AC-1.6.1")
def test_settings__the_end_of_a_turn_triggers_the_check_hook() -> None:
    [(_, command)] = _hook_commands("Stop")

    assert command.endswith("python -m tools.claude_hooks stop")


@pytest.mark.ac("AC-1.6.1")
def test_settings__each_prompt_records_the_start_of_the_turn() -> None:
    [(_, command)] = _hook_commands("UserPromptSubmit")

    assert command.endswith("python -m tools.claude_hooks turn-start")
