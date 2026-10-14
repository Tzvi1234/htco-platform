"""Every action a workflow uses is pinned to a full commit SHA (ER-SEC-09, AC-1.4.3)."""

from pathlib import Path

import pytest

from tools.check_action_pins import main, unpinned_uses

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


@pytest.mark.ac("AC-1.4.3")
@pytest.mark.parametrize(
    "line",
    [
        f"      - uses: actions/checkout@{SHA} # v7.0.1",
        f"    uses: astral-sh/setup-uv@{SHA}",
        f"      - uses: 'actions/checkout@{SHA}'",
        f'      - uses: "actions/checkout@{SHA}"',
        f"    uses: octo-org/shared/.github/workflows/build.yml@{SHA}",
        "      - uses: ./.github/actions/setup",
        "      - uses: docker://alpine@sha256:" + "a" * 64,
    ],
)
def test_unpinned_uses__pinned_or_local_reference__is_accepted(line: str) -> None:
    assert unpinned_uses(f"steps:\n{line}\n") == []


@pytest.mark.ac("AC-1.4.3")
@pytest.mark.parametrize(
    "reference",
    [
        "actions/checkout@v7",
        "actions/checkout@v7.0.1",
        "actions/checkout@main",
        "actions/checkout@3d3c42e",
        "actions/checkout",
        "docker://alpine:3.22",
        f"actions/checkout@{SHA.upper()}",
    ],
)
def test_unpinned_uses__tag_branch_short_sha_or_image_tag__is_reported(reference: str) -> None:
    text = f"steps:\n  - name: Check out\n    uses: {reference}\n"

    assert unpinned_uses(text) == [(3, reference)]


def test_unpinned_uses__commented_out_line__is_ignored() -> None:
    assert unpinned_uses("steps:\n  # - uses: actions/checkout@v7\n") == []


@pytest.mark.ac("AC-1.4.3")
def test_main__workflow_with_unpinned_action__fails_naming_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{SHA}\n      - uses: x/y@v1\n",
        encoding="utf-8",
    )

    exit_code = main(["--root", str(tmp_path)])

    assert exit_code == 1
    assert "ci.yml:5: x/y@v1" in capsys.readouterr().err.replace("\\", "/")


def test_main__composite_action_files__are_checked(tmp_path: Path) -> None:
    action = tmp_path / ".github" / "actions" / "setup" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text("runs:\n  steps:\n    - uses: x/y@main\n", encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 1


def test_main__all_pinned__passes(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(f"steps:\n  - uses: actions/checkout@{SHA}\n", encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 0
