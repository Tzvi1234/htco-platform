"""Fail a pull request whose migrations would apply out of order (AC-5.0.1, audit P1-6).

Supabase's `db push` refuses a migration whose version sorts before the newest one already
applied, and with `--include-all` it applies it out of order. Migrations merge from main and are
applied from main, so the newest version on main is the floor: every migration a pull request
adds must sort after it (and after `--applied`, when the caller knows the latest applied
version). The check also reports malformed names, a version used twice, a migration on main
that was removed or renamed (ER-DATA-05: forward-only), and a pgTAP prefix used twice. Run in CI:

    python -m tools.migration_order --base <base commit>
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: str
    message: str


class GitError(Exception):
    """A git command failed, so the pull request cannot be verified."""

    def __init__(self, args: tuple[str, ...], output: str) -> None:
        super().__init__(f"git {' '.join(args)} failed: {output.strip()}")


MIGRATIONS_DIR = "supabase/migrations"
PGTAP_DIR = "supabase/tests/database"

# A duplicate that predates this check. The second file is renamed once the open Phase 6 pgTAP
# files have taken their numbers; until then it is the only one tolerated.
KNOWN_PGTAP_DUPLICATES: dict[str, frozenset[str]] = {
    "14": frozenset({"14_action_guards.test.sql", "14_worker_order_purge.test.sql"}),
}

_MIGRATION = re.compile(r"^(?P<version>\d{14})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")
_PGTAP = re.compile(r"^(?P<prefix>\d+)_[a-z0-9_]+\.test\.sql$")


def _version(name: str) -> str | None:
    match = _MIGRATION.match(name)
    return match.group("version") if match else None


def order_findings(
    base: Iterable[str], head: Iterable[str], *, applied: str | None = None
) -> list[Finding]:
    """Compare the head's migration file names with the base branch's."""
    base_names = set(base)
    head_names = sorted(set(head))
    findings: list[Finding] = [
        Finding(
            f"{MIGRATIONS_DIR}/{name}",
            "removes or renames a migration that is on main (migrations are forward-only, "
            "ER-DATA-05)",
        )
        for name in sorted(base_names - set(head_names))
    ]
    base_versions = [version for name in base_names if (version := _version(name))]
    floors = []
    if base_versions:
        floors.append((max(base_versions), "the newest migration on main"))
    if applied:
        floors.append((applied, "the latest applied migration"))

    seen: dict[str, str] = {}
    for name in head_names:
        path = f"{MIGRATIONS_DIR}/{name}"
        version = _version(name)
        if version is None:
            findings.append(Finding(path, "is not named <14-digit version>_<snake_case>.sql"))
            continue
        floor_finding = None
        if name not in base_names:
            for floor, label in floors:
                if version <= floor:
                    floor_finding = Finding(
                        path,
                        f"sorts before or with {floor}, {label}; renumber it after that version",
                    )
                    break
        if floor_finding is not None:
            findings.append(floor_finding)
        elif version in seen:
            findings.append(
                Finding(path, f"uses version {version}, already used by {seen[version]}")
            )
        seen.setdefault(version, name)
    return findings


def pgtap_findings(names: Iterable[str]) -> list[Finding]:
    """Report a pgTAP prefix used by more than one test file (the known duplicate aside)."""
    by_prefix: dict[str, list[str]] = {}
    for name in sorted(names):
        match = _PGTAP.match(name)
        if match:
            by_prefix.setdefault(match.group("prefix"), []).append(name)
    findings = []
    for prefix, files in sorted(by_prefix.items()):
        tolerated = KNOWN_PGTAP_DUPLICATES.get(prefix, frozenset())
        first = files[0]
        for name in files[1:]:
            if name in tolerated and first in tolerated:
                continue
            findings.append(
                Finding(
                    f"{PGTAP_DIR}/{name}",
                    f"uses prefix {prefix}, already used by {first}; take the next free one",
                )
            )
    return findings


def _run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if result.returncode != 0:
        raise GitError(args, result.stderr)
    return result.stdout


def main(argv: list[str], *, git: Callable[..., str] = _run_git) -> int:
    parser = argparse.ArgumentParser(description="Check that new migrations sort after main's.")
    parser.add_argument("--base", required=True, help="base commit of the pull request")
    parser.add_argument("--applied", help="latest applied migration version, when known")
    parser.add_argument("--migrations", default=MIGRATIONS_DIR, help="migrations directory")
    parser.add_argument("--pgtap", default=PGTAP_DIR, help="pgTAP test directory")
    args = parser.parse_args(argv)

    try:
        listing = git("ls-tree", "--name-only", args.base, f"{MIGRATIONS_DIR}/")
    except GitError as error:
        sys.stderr.write(f"Cannot verify the migrations: {error}\n")
        return 2
    base = [line.rsplit("/", 1)[-1] for line in listing.splitlines() if line.endswith(".sql")]
    head = [path.name for path in Path(args.migrations).glob("*.sql")]
    pgtap = [path.name for path in Path(args.pgtap).iterdir() if path.is_file()]

    findings = order_findings(base, head, applied=args.applied) + pgtap_findings(pgtap)
    if not findings:
        return 0
    report = "".join(f"  {finding.path}: {finding.message}\n" for finding in findings)
    sys.stderr.write(
        f"Migration order problems:\n{report}"
        "Give new migrations a version after the newest on main (ER-DATA-10).\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
