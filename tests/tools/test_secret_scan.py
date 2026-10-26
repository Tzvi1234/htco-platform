"""The secret scan stays mandatory (SECURITY GATE item 1, ci: scope gitleaks to the PR's own
history). CI's `security` job scans its own run's commits and is required by `ci-gate`; the daily
workflow scans every ref; and an ignored finding is a listed fingerprint, never a disabled rule."""

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github/workflows/ci.yml"
HISTORY = REPO / ".github/workflows/secrets-history.yml"
IGNORE = REPO / ".gitleaksignore"
FINGERPRINT = re.compile(r"^[0-9a-f]{40}:[^:\s]+:[a-z0-9-]+:\d+$")


def _workflow(path: Path) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _gitleaks_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in job["steps"]
        if "gitleaks" in str(step.get("run", "")) and " git " in str(step.get("run", ""))
    ]


def test_ci__the_security_job_is_required_by_the_gate() -> None:
    jobs = _workflow(CI)["jobs"]

    gate = str(jobs["ci-gate"]["steps"][0]["run"])
    [skippable] = re.findall(r"pull_request_only = \{([^}]*)\}", gate)

    assert "security" in jobs["ci-gate"]["needs"]
    assert '"security"' not in skippable  # never skipped, on a push either
    assert "if" not in jobs["security"]  # it runs on every pull request and every push


def test_ci__gitleaks_scans_the_run_and_never_softly() -> None:
    job = _workflow(CI)["jobs"]["security"]
    [step] = _gitleaks_steps(job)
    run = str(step["run"])

    assert "continue-on-error" not in step
    assert "continue-on-error" not in job
    assert "|| true" not in run
    assert "--exit-code" not in run  # the default: a finding fails the step
    assert '--log-opts="$range"' in run
    assert 'range="$BASE_SHA..HEAD"' in run  # a pull request: the commits it brings
    # ...and never an empty range: no base SHA fails the step before the scan (sage-arch).
    assert '[ -n "$BASE_SHA" ] || {' in run
    assert run.index('[ -n "$BASE_SHA" ]') < run.index('range="$BASE_SHA..HEAD"')
    assert "exit 1" in run.split('[ -n "$BASE_SHA" ]', 1)[1].split("\n", 1)[0]
    assert 'range="HEAD"' in run  # a push to main: main's whole history
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert job["steps"][0]["with"]["fetch-depth"] == 0


def test_history__every_ref_is_scanned_daily() -> None:
    workflow = _workflow(HISTORY)
    triggers = workflow[True]  # YAML 1.1 reads the `on:` key as True
    [job] = workflow["jobs"].values()
    [step] = _gitleaks_steps(job)

    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    assert '--log-opts="--all"' in str(step["run"])
    assert "continue-on-error" not in step
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert workflow["permissions"] == {}
    assert job["permissions"] == {"contents": "read"}


def test_ignore__owned_by_the_owner() -> None:
    """No PR whitelists its own leak unseen: CODEOWNERS names the owner for the ignore file."""
    owners = (REPO / ".github/CODEOWNERS").read_text(encoding="utf-8").splitlines()

    assert "/.gitleaksignore @Tzvi1234" in owners


def test_ignore__only_fingerprints_each_with_a_reason() -> None:
    lines = IGNORE.read_text(encoding="utf-8").splitlines()
    entries = [line for line in lines if line.strip() and not line.startswith("#")]

    assert entries, "an empty ignore file belongs deleted"
    for entry in entries:
        assert FINGERPRINT.match(entry), f"not a gitleaks fingerprint: {entry}"
        reason = lines[: lines.index(entry)]
        assert reason, f"{entry} has no reason above it"
        assert reason[-1].startswith("#"), f"{entry} has no reason above it"
