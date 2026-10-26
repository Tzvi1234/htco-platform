"""The one required status, `ci-gate`, waits for every other CI job (Codex and sage on #122).

Branch protection requires only `ci-gate`, so a job missing from its `needs` can fail without
blocking a merge. That nearly happened to the `browser` job, the only place the real-Chromium
test runs. Checked without running CI.
"""

from pathlib import Path
from typing import Any

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = workflow["jobs"]
    return jobs


def test_ci_gate__needs_every_other_job() -> None:
    jobs = _jobs()
    needs = jobs["ci-gate"]["needs"]
    assert sorted(set(jobs) - {"ci-gate"} - set(needs)) == []
    assert "browser" in needs


def test_ci_gate__runs_even_when_a_needed_job_fails() -> None:
    gate = _jobs()["ci-gate"]
    assert gate["if"] == "always()"
    assert gate["permissions"] == {}
