"""Run one CI job's steps locally, read straight from the workflow file, and print a summary.

Made for the `database` job (docs/engineering/LOCAL_DB_TESTS.md): because the steps come from
`ci.yml`, a local run and CI cannot drift apart. Each `run:` step runs the way GitHub runs it
(`bash --noprofile --norc -eo pipefail`, job and step `env`, `RUNNER_TEMP`, `GITHUB_PATH`, `CI`).
`actions/checkout` is skipped (the repository is already here) and `astral-sh/setup-uv` becomes a
check that the pinned uv version is installed. The first failing step stops the job, as in CI.
Run as `python -m tools.local_db_ci [--root DIR] [--job NAME]`; scripts/local-db-ci.sh wraps it.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import yaml

_SKIPPED_ACTIONS = frozenset({"actions/checkout"})
_SETUP_UV = "astral-sh/setup-uv"
_MINUTE = 60
# Keys the runner replays faithfully; anything else (`if`, `shell`, `working-directory`,
# `continue-on-error`, `defaults`, `services`, `container`, `strategy`…) would be replayed wrong,
# so it stops the run instead. `${{ }}` expressions are rejected for the same reason.
_STEP_KEYS = frozenset({"name", "id", "run", "uses", "with", "env", "timeout-minutes"})
_JOB_KEYS = frozenset({"name", "runs-on", "timeout-minutes", "permissions", "env", "steps"})
_EXPRESSION = "${{"


@dataclass(frozen=True)
class WorkflowStep:
    """A step as the workflow declares it: either `run` or `uses` is set."""

    name: str
    run: str | None = None
    uses: str | None = None
    with_: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    env: dict[str, str]
    steps: list[WorkflowStep]


@dataclass(frozen=True)
class Step:
    """A shell step to run locally."""

    name: str
    run: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    name: str
    ran: bool
    passed: bool
    seconds: float


def _strings(mapping: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in (mapping or {}).items()}


def _reject_unsupported(where: str, raw: dict[str, Any], allowed: frozenset[str]) -> None:
    unsupported = sorted(set(raw) - allowed)
    if unsupported:
        raise ValueError(
            f"{where} uses {', '.join(unsupported)}, which tools/local_db_ci cannot replay;"
            " teach the runner before adding it to the job"
        )


def _reject_expressions(where: str, *values: str | None) -> None:
    if any(_EXPRESSION in value for value in values if value is not None):
        raise ValueError(
            f"{where} uses a {_EXPRESSION} }}}} expression, which tools/local_db_ci cannot"
            " evaluate; teach the runner before adding it to the job"
        )


def load_job(workflow_text: str, job_name: str) -> Job:
    """Parse one job (its env includes the workflow's).

    Raises `KeyError` for a missing job and `ValueError` for a feature the runner cannot replay.
    """
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow.get("jobs", {})
    if job_name not in jobs:
        raise KeyError(f"no job {job_name!r} in the workflow (jobs: {', '.join(jobs)})")
    job = jobs[job_name]
    _reject_unsupported(f"job {job_name!r}", job, _JOB_KEYS)
    env = _strings(workflow.get("env")) | _strings(job.get("env"))
    _reject_expressions(f"the env of job {job_name!r}", *env.values())
    steps = []
    for raw in job.get("steps", []):
        uses = raw.get("uses")
        action = uses.split("@", 1)[0] if uses else None
        step = WorkflowStep(
            name=str(raw.get("name") or action or raw.get("run", "").splitlines()[0]),
            run=raw.get("run"),
            uses=action,
            with_=_strings(raw.get("with")),
            env=_strings(raw.get("env")),
        )
        _reject_unsupported(f"step {step.name!r}", raw, _STEP_KEYS)
        _reject_expressions(
            f"step {step.name!r}", step.run, *step.env.values(), *step.with_.values()
        )
        steps.append(step)
    return Job(env=env, steps=steps)


def local_steps(job: Job) -> list[Step]:
    """Turn the job's steps into shell steps; raises `ValueError` for an action we cannot replay."""
    steps = []
    for step in job.steps:
        if step.run is not None:
            steps.append(Step(name=step.name, run=step.run, env=step.env))
        elif step.uses == _SETUP_UV:
            version = step.with_["version"]
            check = (
                f'found="$(uv --version 2> /dev/null | cut -d" " -f2)"\n'
                f'if [ "$found" != "{version}" ]; then\n'
                f'  echo "CI pins uv {version} but found ${{found:-no uv}}:'
                f' run scripts/local-db-setup.sh" >&2\n'
                f"  exit 1\n"
                f"fi\n"
            )
            steps.append(Step(name=f"Check uv {version} (setup-uv)", run=check))
        elif step.uses not in _SKIPPED_ACTIONS:
            raise ValueError(
                f"cannot run the action {step.uses!r} locally; teach tools/local_db_ci"
            )
    return steps


def _stream(process: subprocess.Popen[str], sink: IO[str] | None) -> None:
    assert process.stdout is not None  # noqa: S101 - Popen was given stdout=PIPE
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if sink is not None:
            sink.write(line)


def run_steps(
    steps: list[Step],
    job_env: dict[str, str],
    *,
    cwd: Path,
    log: Path | None = None,
    runner_temp: Path | None = None,
) -> list[StepResult]:
    """Run the steps in order like a GitHub runner; after a failure the rest are skipped."""
    temp = runner_temp or Path(tempfile.mkdtemp(prefix="local-ci-"))
    temp.mkdir(parents=True, exist_ok=True)
    github_path = temp / "github_path"
    github_path.write_text("", encoding="utf-8")
    added_paths: list[str] = []
    results: list[StepResult] = []
    failed = False
    sink = log.open("w", encoding="utf-8") if log is not None else None
    try:
        for number, step in enumerate(steps, start=1):
            if failed:
                results.append(StepResult(step.name, ran=False, passed=False, seconds=0.0))
                continue
            banner = f"\n=== [{number}/{len(steps)}] {step.name}\n"
            sys.stdout.write(banner)
            sys.stdout.flush()
            if sink is not None:
                sink.write(banner)
            script = temp / f"step-{number}.sh"
            script.write_text(step.run, encoding="utf-8")
            env = {
                **os.environ,
                **job_env,
                **step.env,
                "CI": "true",
                "RUNNER_TEMP": str(temp),
                "GITHUB_PATH": str(github_path),
                "PATH": os.pathsep.join([*reversed(added_paths), os.environ.get("PATH", "")]),
            }
            started = time.monotonic()
            with subprocess.Popen(  # noqa: S603 - runs the repository's own workflow steps
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],  # noqa: S607
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            ) as process:
                _stream(process, sink)
            passed = process.returncode == 0
            results.append(StepResult(step.name, True, passed, time.monotonic() - started))
            failed = not passed
            added_paths = [
                line for line in github_path.read_text(encoding="utf-8").splitlines() if line
            ]
    finally:
        if sink is not None:
            sink.close()
    return results


def _duration(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole}s" if whole < _MINUTE else f"{whole // _MINUTE}m{whole % _MINUTE:02d}s"


def _all_passed(results: list[StepResult]) -> bool:
    """A job passes when it ran at least one step and every step passed."""
    return bool(results) and all(result.passed for result in results)


def format_summary(
    results: list[StepResult], *, total_seconds: float, job: str = "database"
) -> str:
    lines = ["", f"--- {job} job, local run ---"]
    for result in results:
        mark = "SKIP" if not result.ran else "PASS" if result.passed else "FAIL"
        timing = f"  ({_duration(result.seconds)})" if result.ran else ""
        lines.append(f"{mark}  {result.name}{timing}")
    verdict = "PASS" if _all_passed(results) else "FAIL"
    lines.append(f"{job} job: {verdict} in {_duration(total_seconds)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a CI job's steps locally.")
    parser.add_argument("--root", type=Path, default=Path(), help="repository root")
    parser.add_argument("--workflow", type=Path, help="workflow file (default: ci.yml in --root)")
    parser.add_argument("--job", default="database", help="job name in the workflow")
    parser.add_argument("--log", type=Path, help="also write every step's output here")
    parser.add_argument("--runner-temp", type=Path, help="RUNNER_TEMP (default: a new directory)")
    args = parser.parse_args(argv)

    workflow = args.workflow or args.root / ".github/workflows/ci.yml"
    job = load_job(workflow.read_text(encoding="utf-8"), args.job)
    started = time.monotonic()
    results = run_steps(
        local_steps(job), job.env, cwd=args.root, log=args.log, runner_temp=args.runner_temp
    )
    sys.stdout.write(
        format_summary(results, total_seconds=time.monotonic() - started, job=args.job)
    )
    return 0 if _all_passed(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
