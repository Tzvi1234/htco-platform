"""Conventional Commits check used by the commit-msg hook and the pr-title CI job (ER-GIT-02)."""

from pathlib import Path

import pytest

from tools.conventional_commit import header_problem, main


@pytest.mark.parametrize(
    "header",
    [
        "feat: add health endpoints",
        "fix(api): return problem details for unknown routes",
        "feat(orders)!: rename order states",
        "chore(deps): bump ruff to 0.17",
        "docs: explain the workspace layout",
        "ci(security): pin actions by commit SHA",
        "test(orders): ingest is idempotent (red)",
        "refactor(model-router): split the price table",
        "build: use uv 0.13",
        "perf: cache the OpenAPI document",
        "revert: feat(api): add diagnostics",
        "style: apply ruff format",
    ],
)
def test_header_problem__conventional_header__is_accepted(header: str) -> None:
    assert header_problem(header) is None


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("Add health endpoints", "type"),
        ("feature: add health endpoints", "type"),
        ("Feat: add health endpoints", "type"),
        ("feat:add health endpoints", "type"),
        ("feat(): add health endpoints", "type"),
        ("feat(API): add health endpoints", "type"),
        ("feat: ", "type"),
        ("feat:   ", "type"),
        ("", "empty"),
        ("feat: " + "x" * 95, "long"),
    ],
)
def test_header_problem__malformed_header__names_the_problem(header: str, reason: str) -> None:
    problem = header_problem(header)

    assert problem is not None
    assert reason in problem


@pytest.mark.parametrize(
    "header",
    [
        "Merge branch 'main' into feat/workspace-tooling",
        'Revert "feat(api): add diagnostics"',
        "fixup! feat: add health endpoints",
        "squash! feat: add health endpoints",
    ],
)
def test_header_problem__git_generated_header__is_accepted(header: str) -> None:
    assert header_problem(header) is None


def test_header_problem__exactly_100_characters__is_accepted() -> None:
    header = "feat: " + "x" * 94

    assert len(header) == 100
    assert header_problem(header) is None


def test_main__message_file_with_comments_and_body__checks_first_line(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(
        "# Please enter the commit message\n\nfeat(api): add health endpoints\n\nBody text.\n",
        encoding="utf-8",
    )

    assert main([str(message)]) == 0


def test_main__invalid_message_file__fails_and_explains(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("updated stuff\n", encoding="utf-8")

    exit_code = main([str(message)])

    assert exit_code == 1
    assert "Conventional Commits" in capsys.readouterr().err


def test_main__title_option__checks_the_title() -> None:
    assert main(["--title", "fix(api): keep request ids"]) == 0
    assert main(["--title", "Fix things"]) == 1
