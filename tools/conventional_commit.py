"""Check that a commit message or pull request title follows Conventional Commits (ER-GIT-02).

Used by the `commit-msg` Git hook (`python -m tools.conventional_commit <message-file>`) and by
the `pr-title` CI job (`python -m tools.conventional_commit --title "<title>"`).
"""

import argparse
import re
import sys
from pathlib import Path

TYPES = (
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
)
MAX_HEADER_LENGTH = 100

_HEADER = re.compile(
    rf"^(?:{'|'.join(TYPES)})"  # type
    r"(?:\([a-z0-9][a-z0-9._-]*\))?"  # optional scope, lower case
    r"!?"  # optional breaking-change marker
    r": \S.*$"  # colon, one space, a non-empty subject
)
# Headers Git writes itself during merges, reverts and autosquash fixups.
_GIT_GENERATED = ("Merge ", 'Revert "', "fixup! ", "squash! ", "amend! ")

_HELP = (
    "Expected a Conventional Commits header such as 'feat(api): add health endpoints'.\n"
    f"Types: {', '.join(TYPES)}. Scope is optional and lower case; "
    f"'!' marks a breaking change; at most {MAX_HEADER_LENGTH} characters."
)


def header_problem(header: str) -> str | None:
    """Return why `header` is not a valid Conventional Commits header, or None when it is."""
    if not header.strip():
        return "The header is empty."
    if header.startswith(_GIT_GENERATED):
        return None
    if len(header) > MAX_HEADER_LENGTH:
        return f"The header is {len(header)} characters long (max {MAX_HEADER_LENGTH})."
    if not _HEADER.match(header):
        return f"The header {header!r} does not start with a valid type, scope and subject."
    return None


def _first_line(message: str) -> str:
    for line in message.splitlines():
        if line.strip() and not line.startswith("#"):
            return line.rstrip()
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check a Conventional Commits header.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("message_file", nargs="?", type=Path, help="commit message file")
    source.add_argument("--title", help="pull request title")
    args = parser.parse_args(argv)

    if args.title is not None:
        header = args.title
    else:
        header = _first_line(args.message_file.read_text(encoding="utf-8"))

    problem = header_problem(header)
    if problem is None:
        return 0
    sys.stderr.write(f"{problem}\n{_HELP}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
