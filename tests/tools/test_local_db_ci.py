"""The local runner replays CI's `database` job from `ci.yml`, so local and CI never drift."""

import shutil
from pathlib import Path

import pytest

from tools.local_db_ci import (
    Step,
    StepResult,
    format_summary,
    load_job,
    local_steps,
    main,
    run_steps,
)

REPO = Path(__file__).resolve().parents[2]

WORKFLOW = """
env:
  WORKFLOW_VALUE: from-workflow
  JOB_VALUE: overridden-by-job
jobs:
  database:
    runs-on: ubuntu-24.04
    env:
      JOB_VALUE: from-job
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install a tool
        run: |
          printf '#!/bin/sh\\necho tool-ran\\n' > "$RUNNER_TEMP/tool"
          chmod +x "$RUNNER_TEMP/tool"
          echo "$RUNNER_TEMP" >> "$GITHUB_PATH"
      - name: Use the tool and the env
        env:
          STEP_VALUE: from-step
        run: |
          tool
          echo "$JOB_VALUE $STEP_VALUE"
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          version: "0.13.0"
      - name: Last step
        run: echo last
  other:
    steps:
      - run: echo other
"""

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _single_step_workflow(tmp_path: Path, run: str) -> Path:
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        f"jobs:\n  database:\n    steps:\n      - name: Only\n        run: {run}\n",
        encoding="utf-8",
    )
    return workflow


def test_load_job__reads_job_env_and_steps_in_order() -> None:
    job = load_job(WORKFLOW, "database")

    assert job.env == {"WORKFLOW_VALUE": "from-workflow", "JOB_VALUE": "from-job"}
    assert [step.name for step in job.steps] == [
        "actions/checkout",
        "Install a tool",
        "Use the tool and the env",
        "astral-sh/setup-uv",
        "Last step",
    ]
    assert job.steps[2].env == {"STEP_VALUE": "from-step"}


def test_load_job__unknown_job__raises_a_clear_error() -> None:
    with pytest.raises(KeyError, match="missing"):
        load_job(WORKFLOW, "missing")


def test_local_steps__skips_checkout_and_checks_the_pinned_uv_version() -> None:
    steps = local_steps(load_job(WORKFLOW, "database"))

    assert [step.name for step in steps] == [
        "Install a tool",
        "Use the tool and the env",
        "Check uv 0.13.0 (setup-uv)",
        "Last step",
    ]
    assert "0.13.0" in steps[2].run


def test_local_steps__unsupported_action__is_rejected() -> None:
    workflow = "jobs:\n  db:\n    steps:\n      - uses: actions/cache@" + "a" * 40 + "\n"

    with pytest.raises(ValueError, match="actions/cache"):
        local_steps(load_job(workflow, "db"))


def test_local_steps__real_ci_database_job__runs_every_ci_command() -> None:
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    steps = local_steps(load_job(workflow, "database"))
    commands = "\n".join(step.run for step in steps)

    assert steps[0].name == "Install the Supabase CLI"
    for command in ("supabase db start", "supabase db reset", "supabase test db", "just test-db"):
        assert command in commands
    assert "registry sync --automations --check" in commands


@needs_bash
def test_run_steps__runs_steps_with_env_and_github_path(tmp_path: Path) -> None:
    steps = local_steps(load_job(WORKFLOW, "database"))
    # The uv check needs a real uv 0.13.0; replace it so the test does not depend on the machine.
    steps[2] = Step(name=steps[2].name, run="true")
    log = tmp_path / "log.txt"

    results = run_steps(steps, {"JOB_VALUE": "from-job"}, cwd=tmp_path, log=log)

    assert [result.passed for result in results] == [True, True, True, True]
    output = log.read_text(encoding="utf-8")
    assert "tool-ran" in output
    assert "from-job from-step" in output


@needs_bash
def test_run_steps__failing_step__stops_and_skips_the_rest(tmp_path: Path) -> None:
    steps = local_steps(load_job(WORKFLOW, "database"))
    steps[1] = Step(name=steps[1].name, run="false\necho not-reached")

    results = run_steps(steps, {}, cwd=tmp_path, log=tmp_path / "log.txt")

    assert [(result.ran, result.passed) for result in results] == [
        (True, True),
        (True, False),
        (False, False),
        (False, False),
    ]
    assert "not-reached" not in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_format_summary__marks_each_step_and_the_overall_result() -> None:
    results = [
        StepResult("Install a tool", ran=True, passed=True, seconds=1.25),
        StepResult("Use the tool", ran=True, passed=False, seconds=62.0),
        StepResult("Last step", ran=False, passed=False, seconds=0.0),
    ]

    summary = format_summary(results, total_seconds=63.25)

    assert "PASS  Install a tool" in summary
    assert "FAIL  Use the tool" in summary
    assert "SKIP  Last step" in summary
    assert "1m02s" in summary
    assert summary.rstrip().endswith("database job: FAIL in 1m03s")


def test_format_summary__all_passed__reports_pass() -> None:
    results = [StepResult("Only", ran=True, passed=True, seconds=5.0)]

    assert format_summary(results, total_seconds=5.0).rstrip().endswith("database job: PASS in 5s")


@needs_bash
def test_main__passing_job__exits_zero_and_prints_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _single_step_workflow(tmp_path, "echo hi")

    code = main(["--workflow", str(workflow), "--root", str(tmp_path)])

    assert code == 0
    assert "database job: PASS" in capsys.readouterr().out


@needs_bash
def test_main__failing_job__exits_one_and_prints_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _single_step_workflow(tmp_path, "exit 1")

    code = main(["--workflow", str(workflow), "--root", str(tmp_path)])

    assert code == 1
    assert "database job: FAIL" in capsys.readouterr().out
