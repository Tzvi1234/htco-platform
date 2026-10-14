"""Test-weakening check: weakened tests need a "Test changes" explanation (ER-TEST-07, AC-1.4.2)."""

from collections.abc import Callable
from textwrap import dedent

import pytest

from tools.test_integrity import (
    Finding,
    config_findings,
    diff_findings,
    explains_test_changes,
    main,
)


def a_diff(path: str, *lines: str, new_file: bool = False, deleted: bool = False) -> str:
    """Build a `git diff --unified=0` section for one file."""
    old = "/dev/null" if new_file else f"a/{path}"
    new = "/dev/null" if deleted else f"b/{path}"
    header = f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- {old}\n+++ {new}\n"
    return header + "@@ -1,2 +1,2 @@\n" + "".join(f"{line}\n" for line in lines)


# --- diff findings ---------------------------------------------------------------------------


@pytest.mark.ac("AC-1.4.2")
@pytest.mark.parametrize(
    "added",
    [
        '+@pytest.mark.skip(reason="flaky")',
        "+    @pytest.mark.skipif(sys.platform == 'win32', reason='later')",
        "+@pytest.mark.xfail",
        '+    pytest.skip("not ready")',
        '+    pytest.xfail("known bug")',
        '+psycopg = pytest.importorskip("psycopg")',
    ],
)
def test_diff_findings__skip_or_xfail_added_in_tests__is_reported(added: str) -> None:
    findings = diff_findings(a_diff("tests/api/test_health.py", added))

    assert findings == [
        Finding("tests/api/test_health.py", "adds a skip or expected-failure marker")
    ]


def test_diff_findings__skip_text_outside_tests__is_ignored() -> None:
    diff = a_diff("docs/engineering/TESTING_STRATEGY.md", "+Never add @pytest.mark.skip to pass.")

    assert diff_findings(diff) == []


def test_diff_findings__test_function_removed__is_reported() -> None:
    diff = a_diff("tests/api/test_health.py", "-def test_healthz__get__returns_ok() -> None:")

    assert diff_findings(diff) == [
        Finding("tests/api/test_health.py", "removes test test_healthz__get__returns_ok")
    ]


def test_diff_findings__async_test_removed__is_reported() -> None:
    diff = a_diff("tests/api/test_health.py", "-async def test_readyz__ok__returns_200():")

    assert diff_findings(diff) == [
        Finding("tests/api/test_health.py", "removes test test_readyz__ok__returns_200")
    ]


def test_diff_findings__test_moved_to_another_file__is_not_reported() -> None:
    diff = a_diff("tests/api/test_old.py", "-def test_moved() -> None:") + a_diff(
        "tests/api/test_new.py", "+def test_moved() -> None:"
    )

    assert diff_findings(diff) == []


def test_diff_findings__assertion_changed__is_reported() -> None:
    diff = a_diff(
        "tests/domain/test_money.py",
        "-    assert total == Decimal('12.34')",
        "+    assert total == Decimal('12.35')",
    )

    assert diff_findings(diff) == [
        Finding("tests/domain/test_money.py", "changes or removes an assertion")
    ]


def test_diff_findings__assertion_moved_unchanged__is_not_reported() -> None:
    diff = a_diff("tests/domain/test_money.py", "-    assert total == 1") + a_diff(
        "tests/domain/test_totals.py", "+        assert total == 1"
    )

    assert diff_findings(diff) == []


def test_diff_findings__deleted_test_file__reports_each_test() -> None:
    diff = a_diff(
        "tests/api/test_health.py",
        "-def test_a() -> None:",
        "-    assert True",
        "-def test_b() -> None:",
        deleted=True,
    )

    assert {f.message for f in diff_findings(diff)} == {
        "removes test test_a",
        "removes test test_b",
        "changes or removes an assertion",
    }


@pytest.mark.parametrize("path", ["justfile", "pyproject.toml"])
def test_diff_findings__coverage_threshold_changed__is_reported(path: str) -> None:
    diff = a_diff(
        path,
        '-    uv run coverage report --include="*/htco_domain/*" --fail-under=95',
        '+    uv run coverage report --include="*/htco_domain/*" --fail-under=90',
    )

    assert diff_findings(diff) == [Finding(path, "changes a coverage threshold")]


def test_diff_findings__new_tests_only__are_not_reported() -> None:
    diff = a_diff(
        "tests/api/test_health.py",
        "+def test_new() -> None:",
        "+    assert 1 + 1 == 2",
        new_file=True,
    )

    assert diff_findings(diff) == []


def test_diff_findings__removed_line_starting_with_dashes__is_not_a_header() -> None:
    diff = a_diff("tests/api/test_cli.py", "--- assert parse('--x') == 1")

    assert diff_findings(diff) == [
        Finding("tests/api/test_cli.py", "changes or removes an assertion")
    ]


# --- configuration findings ------------------------------------------------------------------

BASE_CONFIG = dedent(
    """
    [tool.pytest]
    filterwarnings = ["error"]

    [tool.coverage.report]
    fail_under = 80

    [tool.importlinter]
    root_packages = ["htco_domain"]

    [[tool.importlinter.contracts]]
    id = "dependency-rule"
    type = "layers"
    layers = ["htco_api", "htco_domain"]

    [tool.uv]
    exclude-newer = "7 days"
    """
)


def test_config_findings__unchanged__reports_nothing() -> None:
    assert config_findings(BASE_CONFIG, BASE_CONFIG) == []


def test_config_findings__unrelated_table_changed__reports_nothing() -> None:
    head = BASE_CONFIG.replace('exclude-newer = "7 days"', 'exclude-newer = "5 days"')

    assert config_findings(BASE_CONFIG, head) == []


@pytest.mark.parametrize(
    ("change", "table"),
    [
        (('filterwarnings = ["error"]', 'filterwarnings = ["default"]'), "tool.pytest"),
        (("fail_under = 80", "fail_under = 70"), "tool.coverage"),
        (('layers = ["htco_api", "htco_domain"]', 'layers = ["htco_domain"]'), "tool.importlinter"),
    ],
)
def test_config_findings__quality_gate_table_changed__is_reported(
    change: tuple[str, str], table: str
) -> None:
    head = BASE_CONFIG.replace(*change)

    assert config_findings(BASE_CONFIG, head) == [
        Finding("pyproject.toml", f"changes the {table} quality-gate settings")
    ]


def test_config_findings__no_base_file__reports_nothing() -> None:
    assert config_findings("", BASE_CONFIG) == []


# --- the "Test changes" section --------------------------------------------------------------

TEMPLATE_SECTION = (
    '## Test changes\n\n<!-- Required if any existing test changed. Otherwise write "None". -->\n\n'
)


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "## What and why\n\nStuff.\n",
        TEMPLATE_SECTION + "## Checklist\n",
        TEMPLATE_SECTION + "None\n\n## Checklist\n",
        TEMPLATE_SECTION + "None.\n",
        TEMPLATE_SECTION + "N/A\n",
    ],
)
def test_explains_test_changes__missing_or_empty__is_false(body: str | None) -> None:
    assert explains_test_changes(body) is False


@pytest.mark.parametrize(
    "body",
    [
        TEMPLATE_SECTION + "Removed test_x: the endpoint was deleted in this PR.\n\n## Checklist\n",
        "### Test changes\nThe expected total was wrong: tax is added after the discount.\n",
        "## test changes\r\n\r\nSkipped on Windows until #12 is fixed.\r\n",
    ],
)
def test_explains_test_changes__written_explanation__is_true(body: str) -> None:
    assert explains_test_changes(body) is True


# --- command line ----------------------------------------------------------------------------


def a_git(
    diff: str, base_config: str = BASE_CONFIG, head_config: str = BASE_CONFIG
) -> Callable[..., str]:
    def git(*args: str) -> str:
        if args[0] == "diff":
            return diff
        if args == ("show", "BASE:pyproject.toml"):
            return base_config
        if args == ("show", "HEAD:pyproject.toml"):
            return head_config
        raise AssertionError(f"unexpected git call {args}")

    return git


@pytest.mark.ac("AC-1.4.2")
def test_main__skip_added_without_explanation__fails(capsys: pytest.CaptureFixture[str]) -> None:
    git = a_git(a_diff("tests/api/test_health.py", "+@pytest.mark.skip"))

    exit_code = main(["--base", "BASE"], git=git, environ={"PR_BODY": TEMPLATE_SECTION + "None\n"})

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "tests/api/test_health.py: adds a skip or expected-failure marker" in error
    assert "Test changes" in error


@pytest.mark.ac("AC-1.4.2")
def test_main__skip_added_with_explanation__passes() -> None:
    git = a_git(a_diff("tests/api/test_health.py", "+@pytest.mark.skip"))
    body = TEMPLATE_SECTION + "Skipped until the fixture service exists (#7).\n"

    assert main(["--base", "BASE"], git=git, environ={"PR_BODY": body}) == 0


def test_main__no_findings__passes_without_a_section() -> None:
    git = a_git(a_diff("tests/api/test_health.py", "+def test_new() -> None:"))

    assert main(["--base", "BASE"], git=git, environ={}) == 0


def test_main__config_weakened_without_explanation__fails() -> None:
    head = BASE_CONFIG.replace("fail_under = 80", "fail_under = 10")
    git = a_git("", head_config=head)

    assert main(["--base", "BASE"], git=git, environ={}) == 1
