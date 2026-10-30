"""Every uv that touches the repository is at least 0.13.4: older releases trust the compromised
emSign ECC Root CA - G3 (GHSA-hq85-5cpj-mr62, critical). Checked without running uv."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXED = (0, 13, 4)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _required_floor() -> tuple[int, ...]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = pyproject["tool"]["uv"]["required-version"]
    floor = re.search(r">=\s*([0-9.]+)", required)
    assert floor is not None, f"required-version {required!r} has no lower bound"
    return _version(floor.group(1))


def _setup_uv_versions() -> list[tuple[str, str]]:
    """(workflow:job, version) for every `astral-sh/setup-uv` step."""
    documents = {
        workflow.name: yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for workflow in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    }
    return [
        (f"{workflow}:{name}", str(step["with"]["version"]))
        for workflow, document in documents.items()
        for name, job in (document.get("jobs") or {}).items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
    ]


def test_required_version__refuses_every_uv_that_trusts_the_compromised_root() -> None:
    assert _required_floor() >= FIXED


@pytest.mark.parametrize(("workflow", "version"), _setup_uv_versions())
def test_workflows__set_up_a_fixed_uv(workflow: str, version: str) -> None:
    assert _version(version) >= FIXED, f"{workflow} sets up uv {version}"


def test_dockerfile__builds_with_a_fixed_uv() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    image = re.search(r"^ARG UV_IMAGE=ghcr\.io/astral-sh/uv:([0-9.]+)@sha256:[0-9a-f]{64}$",
                      dockerfile, re.MULTILINE)  # fmt: skip
    assert image is not None, "UV_IMAGE must be a version pinned by digest"
    assert _version(image.group(1)) >= FIXED


# setup-uv verifies the downloaded uv archive against `checksum` (#126). The digests come from
# the GitHub release API (`gh api repos/astral-sh/uv/releases/tags/<version>`, field `digest`)
# and must change together with `version:` in every workflow; every job runs on ubuntu x86_64.
UV_LINUX_X86_64_SHA256 = {
    "0.13.6": "f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645",
}


def _setup_uv_steps() -> list[tuple[str, str, dict[str, object]]]:
    """(workflow:job, runs-on, the step's `with`) for every `astral-sh/setup-uv` step."""
    documents = {
        workflow.name: yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for workflow in sorted((REPO_ROOT / ".github/workflows").glob("*.yml"))
    }
    return [
        (f"{workflow}:{name}", str(job.get("runs-on", "")), dict(step.get("with") or {}))
        for workflow, document in documents.items()
        for name, job in (document.get("jobs") or {}).items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
    ]


@pytest.mark.parametrize(("job", "runs_on", "settings"), _setup_uv_steps())
def test_workflows__verify_the_uv_archive_against_a_pinned_checksum(
    job: str, runs_on: str, settings: dict[str, object]
) -> None:
    assert re.fullmatch(r"ubuntu-[0-9.]+", runs_on), f"{job}: the pin is for ubuntu x86_64 only"
    version = str(settings["version"])
    assert version in UV_LINUX_X86_64_SHA256, f"{job}: no known digest for uv {version}"
    assert settings.get("checksum") == UV_LINUX_X86_64_SHA256[version], (
        f"{job}: setup-uv must verify uv {version} against its pinned SHA-256"
    )
