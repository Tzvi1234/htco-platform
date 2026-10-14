"""Fail when a workflow or composite action uses an action that is not pinned to a commit SHA.

Tags and branches can be moved to malicious code; a full 40-character commit SHA cannot
(ER-SEC-09). Local actions (`./…`) and Docker images pinned by digest are allowed.
Run as `python -m tools.check_action_pins [--root DIR]`.
"""

import argparse
import re
import sys
from pathlib import Path

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<reference>\S+)")
_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_PINNED_IMAGE = re.compile(r"^docker://\S+@sha256:[0-9a-f]{64}$")


def unpinned_uses(text: str) -> list[tuple[int, str]]:
    """Return `(line number, reference)` for every `uses:` that is not pinned."""
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if match is None:
            continue
        reference = match.group("reference").strip("'\"")
        if reference.startswith("./") or _PINNED_IMAGE.match(reference):
            continue
        if not _PINNED_ACTION.match(reference):
            problems.append((number, reference))
    return problems


def _workflow_files(root: Path) -> list[Path]:
    github = root / ".github"
    patterns = (
        "workflows/*.yml",
        "workflows/*.yaml",
        "actions/**/action.yml",
        "actions/**/action.yaml",
    )
    return sorted({path for pattern in patterns for path in github.glob(pattern)})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check that GitHub Actions are pinned by SHA.")
    parser.add_argument("--root", type=Path, default=Path(), help="repository root")
    args = parser.parse_args(argv)

    problems = [
        f"{path}:{number}: {reference}"
        for path in _workflow_files(args.root)
        for number, reference in unpinned_uses(path.read_text(encoding="utf-8"))
    ]
    if not problems:
        return 0
    sys.stderr.write("Actions must be pinned to a full commit SHA (add the tag as a comment):\n")
    sys.stderr.writelines(f"  {problem}\n" for problem in problems)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
