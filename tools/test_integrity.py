"""Flag pull requests that weaken tests without a "Test changes" explanation (ER-TEST-07).

Findings: skip or expected-failure markers added to tests, test functions removed, assertions
changed or removed, coverage thresholds changed, and any change to the quality-gate settings in
`pyproject.toml`. A finding fails the check unless the pull request body has a "Test changes"
section that explains it. Run in CI as:

    PR_BODY=… python -m tools.test_integrity --base <base commit>
"""

import argparse
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    path: str
    message: str


SKIP_MARKER = "adds a skip or expected-failure marker"
CHANGED_ASSERTION = "changes or removes an assertion"
CHANGED_THRESHOLD = "changes a coverage threshold"

_SKIP = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail|importorskip)\(")
_TEST_FUNCTION = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)")
_ASSERTION = re.compile(r"\bassert\b")
_THRESHOLD = re.compile(r"fail[-_]under")
_QUALITY_GATE_TABLES = (
    ("tool", "pytest"),
    ("tool", "coverage"),
    ("tool", "importlinter"),
    ("tool", "mypy"),
    ("tool", "ruff", "lint"),
)


@dataclass(frozen=True)
class _Line:
    path: str
    text: str
    added: bool


def _changed_lines(diff: str) -> Iterator[_Line]:
    """Yield added and removed lines of a unified diff with the file they belong to."""
    old_path = new_path = ""
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
        elif in_header and line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif in_header and line.startswith("+++ "):
            new_path = line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            in_header = False
        elif not in_header and line[:1] in {"+", "-"}:
            added = line.startswith("+")
            path = new_path if added or new_path != "/dev/null" else old_path
            yield _Line(path=path, text=line[1:], added=added)


def _is_test_file(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def diff_findings(diff: str) -> list[Finding]:
    """Find weakened tests and thresholds in `git diff --unified=0` output."""
    lines = list(_changed_lines(diff))
    test_lines = [line for line in lines if _is_test_file(line.path)]
    findings: list[Finding] = []

    findings += [
        Finding(line.path, SKIP_MARKER)
        for line in test_lines
        if line.added and _SKIP.search(line.text)
    ]

    added_tests = {
        m.group(1) for line in test_lines if line.added and (m := _TEST_FUNCTION.match(line.text))
    }
    for line in test_lines:
        match = _TEST_FUNCTION.match(line.text)
        if not line.added and match and match.group(1) not in added_tests:
            findings.append(Finding(line.path, f"removes test {match.group(1)}"))

    added_assertions = {line.text.strip() for line in test_lines if line.added}
    findings += [
        Finding(line.path, CHANGED_ASSERTION)
        for line in test_lines
        if not line.added
        and _ASSERTION.search(line.text)
        and line.text.strip() not in added_assertions
    ]

    findings += [
        Finding(line.path, CHANGED_THRESHOLD)
        for line in lines
        if not line.added
        and line.path in {"justfile", "pyproject.toml"}
        and _THRESHOLD.search(line.text)
    ]
    return list(dict.fromkeys(findings))


def _table(config: Mapping[str, Any], keys: tuple[str, ...]) -> object:
    node: object = config
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def config_findings(base_pyproject: str, head_pyproject: str) -> list[Finding]:
    """Report every quality-gate table in `pyproject.toml` that differs from the base."""
    if not base_pyproject.strip():
        return []
    base, head = tomllib.loads(base_pyproject), tomllib.loads(head_pyproject)
    return [
        Finding("pyproject.toml", f"changes the {'.'.join(keys)} quality-gate settings")
        for keys in _QUALITY_GATE_TABLES
        if _table(base, keys) != _table(head, keys)
    ]


_SECTION = re.compile(
    r"^#{2,3}\s*test changes\s*$(?P<text>.*?)(?=^#{1,3}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EMPTY_ANSWERS = {"", "none", "n/a", "na", "-"}


def explains_test_changes(body: str | None) -> bool:
    """True when the pull request body has a non-empty "Test changes" section."""
    match = _SECTION.search((body or "").replace("\r\n", "\n"))
    if match is None:
        return False
    text = _COMMENT.sub("", match.group("text")).strip().rstrip(".").lower()
    return text not in _EMPTY_ANSWERS


def _run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout if result.returncode == 0 else ""


def main(
    argv: list[str],
    *,
    git: Callable[..., str] = _run_git,
    environ: Mapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser(description="Flag weakened tests in a pull request.")
    parser.add_argument("--base", required=True, help="base commit of the pull request")
    args = parser.parse_args(argv)

    diff = git(
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--find-renames",
        f"{args.base}...HEAD",
    )
    findings = diff_findings(diff) + config_findings(
        git("show", f"{args.base}:pyproject.toml"), git("show", "HEAD:pyproject.toml")
    )
    if not findings:
        return 0

    report = "".join(f"  {finding.path}: {finding.message}\n" for finding in findings)
    if explains_test_changes(environ.get("PR_BODY")):
        sys.stderr.write(f"Test changes found and explained in the pull request:\n{report}")
        return 0
    sys.stderr.write(
        f"Test changes found:\n{report}"
        "Explain them in the pull request's 'Test changes' section (ER-TEST-07).\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
