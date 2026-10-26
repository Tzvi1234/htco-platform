"""The local runner replays CI's `database` job from `ci.yml`, so local and CI never drift."""

import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import override

import pytest
import yaml

from tools.local_db_ci import (
    Step,
    StepResult,
    _tail,
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


def _workflow(job: dict[str, object]) -> str:
    return yaml.safe_dump({"jobs": {"db": job}})


@pytest.mark.parametrize(
    ("step", "problem"),
    [
        ({"run": "echo hi", "if": "github.event_name == 'push'"}, "if"),
        ({"run": "ls", "working-directory": "apps"}, "working-directory"),
        ({"run": "echo hi", "shell": "pwsh"}, "shell"),
        ({"run": "exit 1", "continue-on-error": True}, "continue-on-error"),
        ({"run": "echo ${{ github.sha }}"}, "${{"),
        ({"run": "echo", "env": {"SHA": "${{ github.sha }}"}}, "${{"),
        ({"uses": "astral-sh/setup-uv@" + "a" * 40, "with": {"version": "${{ env.V }}"}}, "${{"),
    ],
)
def test_load_job__step_feature_the_runner_cannot_replay__is_rejected(
    step: dict[str, object], problem: str
) -> None:
    workflow = _workflow({"steps": [{"name": "Step", **step}]})

    with pytest.raises(ValueError, match=re.escape(problem)):
        load_job(workflow, "db")


@pytest.mark.parametrize(
    ("setting", "problem"),
    [
        ({"if": "always()"}, "if"),
        ({"defaults": {"run": {"shell": "sh"}}}, "defaults"),
        ({"services": {"pg": {"image": "postgres"}}}, "services"),
        ({"container": "ubuntu"}, "container"),
        ({"strategy": {"matrix": {"x": [1]}}}, "strategy"),
        ({"env": {"A": "${{ secrets.A }}"}}, "${{"),
    ],
)
def test_load_job__job_feature_the_runner_cannot_replay__is_rejected(
    setting: dict[str, object], problem: str
) -> None:
    workflow = _workflow({**setting, "steps": [{"run": "echo hi"}]})

    with pytest.raises(ValueError, match=re.escape(problem)):
        load_job(workflow, "db")


def test_load_job__workflow_env_with_an_expression__is_rejected() -> None:
    workflow = "env:\n  A: ${{ vars.A }}\n" + _workflow({"steps": [{"run": "echo hi"}]})

    with pytest.raises(ValueError, match=re.escape("${{")):
        load_job(workflow, "db")


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


needs_flock = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("bash") is None, reason="needs bash and flock"
)


def _fake_tool(directory: Path, name: str, body: str) -> None:
    tool = directory / name
    tool.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    tool.chmod(0o755)


# The wrapper's markers in its cache: the cache is the runner's, and the stack it left running.
OWNER_MARKER = ".htco-local-db-ci"
STACK_MARKER = "stack-started"


def _fake_docker(bin_dir: Path, tmp_path: Path, *, running: bool) -> None:
    """`docker ps` lists one container of the project while a stack runs, until `supabase stop`
    is called (the fake supabase records its calls in supabase-calls.txt)."""
    log = tmp_path / "docker-calls.txt"
    stops = tmp_path / "supabase-calls.txt"
    listing = "c0ffee" if running else ""
    _fake_tool(
        bin_dir,
        "docker",
        f'echo "$*" >> "{log}"\n'
        f'if [ "$1" = ps ] && [ -n "{listing}" ] && ! grep -qs stop "{stops}"; then\n'
        f"  echo {listing}\nfi\nexit 0",
    )


def _wrapper_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("jobs: {}\n", encoding="utf-8")
    (repo / "supabase").mkdir()
    (repo / "supabase/config.toml").write_text('project_id = "htco-platform"\n', encoding="utf-8")
    return repo


def _run_wrapper(
    repo: Path, bin_dir: Path, cache: Path | str, *args: str, home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO / "scripts/local-db-ci.sh"), str(repo), *args],  # noqa: S607
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HTCO_LOCAL_CI_CACHE": str(cache),
            **({"HOME": str(home)} if home is not None else {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@needs_flock
def test_wrapper__stack_kept_by_an_earlier_run__is_stopped_before_its_cli_is_removed(
    tmp_path: Path,
) -> None:
    # Given a run with --keep-running left the stack up, with the CLI only in the runner directory
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("jobs: {}\n", encoding="utf-8")
    (repo / "supabase").mkdir()
    (repo / "supabase/config.toml").write_text('project_id = "htco-platform"\n', encoding="utf-8")
    cache = tmp_path / "cache"
    (cache / "runner").mkdir(parents=True)
    (cache / OWNER_MARKER).write_text("", encoding="utf-8")
    (cache / STACK_MARKER).write_text("htco-platform\n", encoding="utf-8")
    calls = tmp_path / "supabase-calls.txt"
    _fake_tool(cache / "runner", "supabase", f'echo "$*" >> "{calls}"')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_tool(bin_dir, "uv", "exit 0")
    _fake_docker(bin_dir, tmp_path, running=True)

    # When the next run starts
    result = subprocess.run(
        ["bash", str(REPO / "scripts/local-db-ci.sh"), str(repo)],  # noqa: S607
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HTCO_LOCAL_CI_CACHE": str(cache),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    # Then the kept stack was stopped first
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines()[0] == "stop --no-backup"


def test_main__job_without_steps__exits_one_like_its_fail_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = tmp_path / "ci.yml"
    workflow.write_text(yaml.safe_dump({"jobs": {"database": {"steps": []}}}), encoding="utf-8")

    code = main(["--workflow", str(workflow), "--root", str(tmp_path)])

    assert code == 1
    assert "database job: FAIL" in capsys.readouterr().out


# --- timeout-minutes (#114): a hung step ends, and so does everything it started -------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (dead, not yet reaped) does not count.
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text(encoding="utf-8").split()[2] == "Z")


def test_load_job__reads_step_and_job_timeouts() -> None:
    job = load_job(
        "jobs:\n  database:\n    timeout-minutes: 20\n    steps:\n"
        "      - name: Bounded\n        timeout-minutes: 3\n        run: echo a\n"
        "      - name: Unbounded\n        run: echo b\n",
        "database",
    )

    assert job.timeout_minutes == 20
    assert [s.timeout_minutes for s in local_steps(job)] == [3, None]


def test_load_job__without_a_job_timeout__uses_githubs_default_of_360_minutes() -> None:
    assert load_job("jobs:\n  database:\n    steps: []\n", "database").timeout_minutes == 360


needs_posix_bash = pytest.mark.skipif(
    shutil.which("bash") is None or os.name != "posix", reason="needs bash and POSIX signals"
)


@needs_posix_bash
def test_run_steps__step_past_its_timeout__killed_with_its_children_and_the_rest_skipped(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    steps = [
        Step(
            name="Hangs",
            # A child that would outlive its step, and a step that never ends on its own.
            run=f'sleep 300 &\necho $! > "{pid_file}"\nsleep 300\n',
            timeout_minutes=0.02,  # 1.2 seconds
        ),
        Step(name="After", run="echo not-reached"),
    ]

    started = time.monotonic()
    results = run_steps(steps, {}, cwd=tmp_path, log=tmp_path / "log.txt")
    elapsed = time.monotonic() - started

    assert elapsed < 30
    assert [(r.ran, r.passed, r.timed_out) for r in results] == [
        (True, False, True),
        (False, False, False),
    ]
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "the step's child outlived the timeout"
    assert "not-reached" not in (tmp_path / "log.txt").read_text(encoding="utf-8")


@needs_posix_bash
def test_run_steps__the_job_timeout_bounds_a_step_without_its_own(tmp_path: Path) -> None:
    started = time.monotonic()
    results = run_steps(
        [Step(name="Hangs", run="sleep 300")], {}, cwd=tmp_path, job_timeout_minutes=0.02
    )

    assert time.monotonic() - started < 30
    assert results[0].timed_out


@needs_posix_bash
def test_run_steps__a_step_that_ignores_sigterm__is_killed(tmp_path: Path) -> None:
    started = time.monotonic()
    results = run_steps(
        [Step(name="Stubborn", run="trap '' TERM\nwhile true; do sleep 1; done",
              timeout_minutes=0.02)],
        {}, cwd=tmp_path,
    )  # fmt: skip

    assert time.monotonic() - started < 30
    assert results[0].timed_out


def test_format_summary__a_timed_out_step__says_so() -> None:
    results = [StepResult("Hangs", ran=True, passed=False, seconds=1200.0, timed_out=True)]

    summary = format_summary(results, total_seconds=1200.0)

    assert "TIME  Hangs  (20m00s, timed out)" in summary
    assert summary.rstrip().endswith("database job: FAIL in 20m00s")


@needs_flock
def test_wrapper__a_process_left_by_the_run__does_not_keep_the_lock(tmp_path: Path) -> None:
    """A step's leftover process must not inherit the lock: the next agent's run would wait for
    it forever (this happened once, across a PC sleep)."""
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("jobs: {}\n", encoding="utf-8")
    (repo / "supabase").mkdir()
    (repo / "supabase/config.toml").write_text('project_id = "htco-platform"\n', encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    pid_file = tmp_path / "leftover.pid"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The runner (through uv) leaves a process behind, as a killed step's child would.
    _fake_tool(bin_dir, "uv", f'sleep 300 > /dev/null 2>&1 &\necho $! > "{pid_file}"\nexit 0')
    _fake_tool(bin_dir, "docker", "exit 0")

    result = subprocess.run(
        ["bash", str(REPO / "scripts/local-db-ci.sh"), str(repo)],  # noqa: S607
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HTCO_LOCAL_CI_CACHE": str(cache),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    leftover = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        free = subprocess.run(
            ["flock", "--nonblock", str(cache / "lock"), "true"],  # noqa: S607
            check=False,
        )
        assert free.returncode == 0, "the leftover process still holds the lock"
    finally:
        os.kill(leftover, signal.SIGTERM)


def test_setup__pins_the_sha256_of_the_uv_that_ci_uses() -> None:
    """The setup script trusts only its own pin; it must follow CI's uv version (#114)."""
    setup = (REPO / "scripts/local-db-setup.sh").read_text(encoding="utf-8")
    pinned = re.search(r'^uv_pinned_version="([0-9.]+)"$', setup, re.MULTILINE)
    digest = re.search(r'^uv_sha256_x86_64="([0-9a-f]{64})"$', setup, re.MULTILINE)

    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    versions = {
        step["with"]["version"]
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
    }

    assert pinned is not None
    assert digest is not None
    assert versions == {pinned.group(1)}
    assert '.sha256"' not in setup  # never the release's own checksum file


# --- Codex (local) on #99, P1: never stop a Supabase stack the script did not start ----------


def _wrapper_tools(tmp_path: Path, *, running: bool) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "supabase-calls.txt"
    _fake_tool(bin_dir, "supabase", f'echo "$*" >> "{calls}"')
    _fake_tool(bin_dir, "uv", "exit 0")
    _fake_docker(bin_dir, tmp_path, running=running)
    return bin_dir, calls


@needs_flock
def test_wrapper__a_stack_it_did_not_start__left_alone_and_the_run_refused(tmp_path: Path) -> None:
    # Given a developer's own stack (`just db-start`) is running, with no marker of this script
    repo = _wrapper_repo(tmp_path)
    bin_dir, calls = _wrapper_tools(tmp_path, running=True)

    result = _run_wrapper(repo, bin_dir, tmp_path / "cache")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "did not start" in result.stderr
    assert not calls.exists(), "the developer's stack was stopped"


@needs_flock
def test_wrapper__no_stack_running__stops_nothing_before_the_run(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, calls = _wrapper_tools(tmp_path, running=False)

    result = _run_wrapper(repo, bin_dir, tmp_path / "cache", "--keep-running")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()


@needs_flock
def test_wrapper__keep_running__marks_the_stack_as_its_own(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, _ = _wrapper_tools(tmp_path, running=False)
    cache = tmp_path / "cache"

    result = _run_wrapper(repo, bin_dir, cache, "--keep-running")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (cache / STACK_MARKER).read_text(encoding="utf-8").strip() == "htco-platform"


@needs_flock
def test_wrapper__a_normal_run__stops_its_stack_and_drops_the_marker(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, calls = _wrapper_tools(tmp_path, running=False)
    cache = tmp_path / "cache"

    result = _run_wrapper(repo, bin_dir, cache)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == ["stop --no-backup"]
    assert not (cache / STACK_MARKER).exists()


# --- Codex (local) on #99, P2: the cache it deletes in, as root, must be its own --------------


@needs_flock
@pytest.mark.parametrize("where", ["root", "home", "mnt-c", "repo", "inside-repo", "above-repo"])
def test_wrapper__a_cache_path_that_is_not_its_own__refused_before_anything_is_touched(
    tmp_path: Path, where: str
) -> None:
    repo = _wrapper_repo(tmp_path)
    (repo / "runner").mkdir()
    (repo / "runner/keep.txt").write_text("mine", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "runner").mkdir()
    bin_dir, _ = _wrapper_tools(tmp_path, running=False)
    link = tmp_path / "link-to-root"
    link.symlink_to("/")
    cache = {
        "root": link,  # a symlink: the check must canonicalize
        "home": home,
        "mnt-c": Path("/mnt/c"),
        "repo": repo,
        "inside-repo": repo / ".cache",
        "above-repo": tmp_path,
    }[where]

    result = _run_wrapper(repo, bin_dir, cache, home=home)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "HTCO_LOCAL_CI_CACHE" in result.stderr
    assert (repo / "runner/keep.txt").exists()
    assert (home / "runner").exists()
    assert not (home / "lock").exists()


@needs_flock
def test_wrapper__a_non_empty_directory_without_its_marker__refused(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, _ = _wrapper_tools(tmp_path, running=False)
    cache = tmp_path / "somebody-elses"
    (cache / "runner").mkdir(parents=True)
    (cache / "runner/data.txt").write_text("theirs", encoding="utf-8")

    result = _run_wrapper(repo, bin_dir, cache)

    assert result.returncode == 2, result.stdout + result.stderr
    assert (cache / "runner/data.txt").exists()


@needs_flock
def test_wrapper__a_new_cache__created_with_its_marker(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, _ = _wrapper_tools(tmp_path, running=False)
    cache = tmp_path / "new-cache"

    result = _run_wrapper(repo, bin_dir, cache)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (cache / OWNER_MARKER).exists()


# --- Codex (local) on #99, P2: uv for the machine's architecture, each pinned ---------------


@needs_bash
def test_setup__x86_64__picks_the_pinned_uv_archive(tmp_path: Path) -> None:
    _fake_tool(tmp_path, "uname", "echo x86_64")

    result = subprocess.run(
        ["bash", str(REPO / "scripts/local-db-setup.sh"), "--uv-asset"],  # noqa: S607
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    name, digest = result.stdout.split()
    assert name == "uv-x86_64-unknown-linux-gnu.tar.gz"
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


@needs_bash
@pytest.mark.parametrize("machine", ["aarch64", "arm64", "armv7l"])
def test_setup__an_architecture_ci_cannot_replay__refused_before_installing(
    tmp_path: Path, machine: str
) -> None:
    """CI's database job installs the linux_amd64 Supabase CLI, so on ARM the local run could
    never work: refuse there, before apt or uv touch the machine (Codex on #99)."""
    _fake_tool(tmp_path, "uname", f"echo {machine}")
    _fake_tool(tmp_path, "apt-get", f'echo apt-get >> "{tmp_path}/installed.txt"')

    result = subprocess.run(
        ["bash", str(REPO / "scripts/local-db-setup.sh"), "--uv-asset"],  # noqa: S607
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert machine in result.stderr
    assert "linux_amd64" in result.stderr
    assert not (tmp_path / "installed.txt").exists()


@needs_bash
@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_wrapper__on_arm__refused_with_the_reason(tmp_path: Path, machine: str) -> None:
    repo = _wrapper_repo(tmp_path)
    bin_dir, _ = _wrapper_tools(tmp_path, running=False)
    _fake_tool(bin_dir, "uname", f"echo {machine}")

    result = _run_wrapper(repo, bin_dir, tmp_path / "cache")

    assert result.returncode == 2
    assert "linux_amd64" in result.stderr


# --- Codex on #99 (e9c8a7a): output cleanup is bounded too --------------------------------


@needs_posix_bash
@pytest.mark.parametrize("timeout_minutes", [None, 0.02])
def test_run_steps__a_detached_child_holding_the_output__does_not_hold_the_runner(
    tmp_path: Path, timeout_minutes: float | None
) -> None:
    """`setsid` puts the child outside the step's process group, so the kill cannot reach it,
    and it keeps the step's output open for 60 s. The runner must still return promptly."""
    pid_file = tmp_path / "detached.pid"
    hang = "" if timeout_minutes is None else "sleep 300\n"
    step = Step(
        name="Leaves a detached child",
        run=f'setsid sleep 60 &\necho $! > "{pid_file}"\necho step-output\n{hang}',
        timeout_minutes=timeout_minutes,
    )
    log = tmp_path / "log.txt"

    started = time.monotonic()
    try:
        results = run_steps([step], {}, cwd=tmp_path, log=log)
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)

    assert elapsed < 20, f"the runner waited {elapsed:.0f}s for the detached child"
    assert results[0].timed_out is (timeout_minutes is not None)
    assert "step-output" in log.read_text(encoding="utf-8")


class _SlowSink(io.StringIO):
    """A log that takes 5 ms per write, so the writer below always gets ahead of the reader."""

    @override
    def write(self, text: str) -> int:
        time.sleep(0.005)
        return super().write(text)


def test_tail__a_writer_that_never_stops__drained_only_to_where_the_step_ended(
    tmp_path: Path,
) -> None:
    """After the step ends, the output is read up to the size it had then: a leftover process
    that keeps writing must not keep the runner reading forever (quill, #99)."""
    output = tmp_path / "step-1.out"
    output.write_bytes(b"step-output\n")
    stop_writer = threading.Event()

    def leftover() -> None:  # small, steady writes: about 20 KB/s, never a disk filler
        with output.open("ab", buffering=0) as sink:
            while not stop_writer.is_set():
                sink.write(b"spam\n")
                time.sleep(0.0005)

    done = threading.Event()
    log = _SlowSink()
    writer = threading.Thread(target=leftover, daemon=True)
    tail = threading.Thread(target=_tail, args=(output, done, log), daemon=True)
    try:
        writer.start()
        tail.start()
        time.sleep(0.2)
        done.set()  # the step has ended
        tail.join(timeout=10)
        still_reading = tail.is_alive()
    finally:
        stop_writer.set()
        writer.join(timeout=5)
        tail.join(timeout=30)

    assert not still_reading, "the runner kept reading a leftover process's output"
    assert log.getvalue().startswith("step-output\n")


# --- Codex on #99 (d29c2c5): an interrupted runner stops its step first ---------------------


@needs_posix_bash
def test_run_steps__interrupted__stops_the_step_and_its_children_before_raising(
    tmp_path: Path,
) -> None:
    """Ctrl-C reaches only the runner (the step has its own session): the step's group must be
    stopped before the interrupt propagates, or it outlives the run and its lock."""
    pid_file = tmp_path / "child.pid"
    step = Step(name="Long", run=f'sleep 300 &\necho $! > "{pid_file}"\nsleep 300\n')

    def interrupt_once_started() -> None:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_once_started, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_steps([step], {}, cwd=tmp_path, log=tmp_path / "log.txt")

    assert time.monotonic() - started < 30
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        assert not _alive(child), "the step's child outlived the interrupted runner"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
