"""Image policy checked without Docker: pinned bases, non-root runtime, minimal build context.

CI builds, runs and scans the real image (the `image` job); these tests catch the same
mistakes in a second, before a build.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _base_images() -> list[str]:
    declared = re.findall(r"^ARG\s+\w+_IMAGE=(\S+)", DOCKERFILE, re.MULTILINE)
    literal = [
        image
        for image in re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.MULTILINE)
        if not image.startswith("${")
    ]
    return declared + literal


def _final_stage() -> str:
    return re.split(r"^FROM\s", DOCKERFILE, flags=re.MULTILINE)[-1]


@pytest.mark.ac("AC-1.5.2")
def test_dockerfile__runtime_stage__runs_as_a_non_root_user() -> None:
    users = re.findall(r"^USER\s+(\S+)", _final_stage(), re.MULTILINE)

    assert users, "the runtime stage must set USER"
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_dockerfile__every_base_image__is_pinned_by_digest() -> None:
    images = _base_images()

    assert images
    for image in images:
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image


def test_dockerfile__installs_only_the_api_without_dev_tools() -> None:
    assert re.search(r"uv sync --locked --no-dev --no-editable --package htco-api", DOCKERFILE), (
        "the image must install the locked API package only"
    )


def test_dockerignore__allows_only_the_build_inputs() -> None:
    rules = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert rules[0] == "*", "the build context must be an allow-list"
    allowed = {rule[1:].rstrip("/") for rule in rules if rule.startswith("!")}
    assert allowed == {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "apps",
        "packages",
        "workers",
    }
