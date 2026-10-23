"""Migration-order guard: a new migration sorts after every migration on main (AC-5.0.1)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from tools.migration_order import (
    Finding,
    GitError,
    main,
    order_findings,
    pgtap_findings,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "supabase" / "migrations"
PGTAP = REPO / "supabase" / "tests" / "database"

MAIN = [
    "20261022120000_etsy_order_history.sql",
    "20261023120000_job_queue.sql",
    "20261024220000_order_reconciliation.sql",
]


# --- order findings ---------------------------------------------------------------------------


@pytest.mark.ac("AC-5.0.1")
def test_order_findings__new_migration_after_newest_on_main__passes() -> None:
    head = [*MAIN, "20261024230000_orders_api.sql"]

    assert order_findings(MAIN, head) == []


@pytest.mark.ac("AC-5.0.1")
@pytest.mark.parametrize(
    "name",
    [
        "20261024210500_late.sql",  # between two migrations on main
        "20261024220000_same_version.sql",  # equal to the newest on main
        "20261001000000_old.sql",  # older than everything
    ],
)
def test_order_findings__new_migration_not_after_newest_on_main__is_reported(name: str) -> None:
    findings = order_findings(MAIN, [*MAIN, name])

    assert findings == [
        Finding(
            f"supabase/migrations/{name}",
            "sorts before or with 20261024220000, the newest migration on main; "
            "renumber it after that version",
        )
    ]


@pytest.mark.ac("AC-5.0.1")
def test_order_findings__new_migration_not_after_latest_applied__is_reported() -> None:
    head = [*MAIN, "20261024230000_orders_api.sql"]

    findings = order_findings(MAIN, head, applied="20261025000000")

    assert findings == [
        Finding(
            "supabase/migrations/20261024230000_orders_api.sql",
            "sorts before or with 20261025000000, the latest applied migration; "
            "renumber it after that version",
        )
    ]


@pytest.mark.ac("AC-5.0.1")
def test_order_findings__several_new_migrations_in_order__pass() -> None:
    head = [*MAIN, "20261024230000_a.sql", "20261024231000_b.sql"]

    assert order_findings(MAIN, head) == []


def test_order_findings__removed_or_renamed_migration_on_main__is_reported() -> None:
    head = [MAIN[0], "20261023120000_job_queue_v2.sql", MAIN[2]]

    findings = order_findings(MAIN, head)

    assert (
        Finding(
            "supabase/migrations/20261023120000_job_queue.sql",
            "removes or renames a migration that is on main "
            "(migrations are forward-only, ER-DATA-05)",
        )
        in findings
    )


@pytest.mark.parametrize(
    "name",
    ["2026102423_short.sql", "20261024230000-dash.sql", "20261024230000_Upper.sql", "notes.sql"],
)
def test_order_findings__malformed_name__is_reported(name: str) -> None:
    findings = order_findings(MAIN, [*MAIN, name])

    assert findings == [
        Finding(
            f"supabase/migrations/{name}",
            "is not named <14-digit version>_<snake_case>.sql",
        )
    ]


def test_order_findings__two_files_with_one_version__are_reported() -> None:
    head = [*MAIN, "20261024230000_a.sql", "20261024230000_b.sql"]

    findings = order_findings(MAIN, head)

    assert findings == [
        Finding(
            "supabase/migrations/20261024230000_b.sql",
            "uses version 20261024230000, already used by 20261024230000_a.sql",
        )
    ]


def test_order_findings__first_migration_on_an_empty_main__passes() -> None:
    assert order_findings([], ["20261014130000_base_schemas.sql"]) == []


# --- pgTAP prefixes ---------------------------------------------------------------------------


def test_pgtap_findings__unique_prefixes__pass() -> None:
    assert pgtap_findings(["00_schemas.test.sql", "01_identity.test.sql", "__init__.py"]) == []


def test_pgtap_findings__new_duplicate_prefix__is_reported() -> None:
    findings = pgtap_findings(["18_order_reconciliation.test.sql", "18_orders_api.test.sql"])

    assert findings == [
        Finding(
            "supabase/tests/database/18_orders_api.test.sql",
            "uses prefix 18, already used by 18_order_reconciliation.test.sql; "
            "take the next free one",
        )
    ]


# --- the repository as it is ------------------------------------------------------------------


@pytest.mark.ac("AC-5.0.1")
def test_repository__migrations_are_well_formed_and_unique() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))

    assert names, "no migrations found"
    assert order_findings([], names) == []


def test_repository__pgtap_prefixes_are_unique() -> None:
    assert pgtap_findings(sorted(path.name for path in PGTAP.iterdir())) == []


# --- main -------------------------------------------------------------------------------------


def fake_git(base: list[str]) -> Callable[..., str]:
    def git(*args: str) -> str:
        assert args[:3] == ("ls-tree", "--name-only", "abc123")
        return "".join(f"supabase/migrations/{name}\n" for name in base)

    return git


@pytest.mark.ac("AC-5.0.1")
def test_main__in_order__exits_zero(tmp_path: Path) -> None:
    for name in [*MAIN, "20261024230000_orders_api.sql"]:
        (tmp_path / name).write_text("select 1;\n", encoding="utf-8")

    status = main(
        ["--base", "abc123", "--migrations", str(tmp_path), "--pgtap", str(tmp_path)],
        git=fake_git(MAIN),
    )

    assert status == 0


@pytest.mark.ac("AC-5.0.1")
def test_main__out_of_order__exits_one_and_names_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in [*MAIN, "20261024210500_late.sql"]:
        (tmp_path / name).write_text("select 1;\n", encoding="utf-8")

    status = main(
        ["--base", "abc123", "--migrations", str(tmp_path), "--pgtap", str(tmp_path)],
        git=fake_git(MAIN),
    )

    assert status == 1
    assert "20261024210500_late.sql" in capsys.readouterr().err


def test_main__git_fails__exits_two_rather_than_pass(tmp_path: Path) -> None:
    def broken_git(*args: str) -> str:
        raise GitError(args, "fatal: bad object abc123")

    status = main(
        ["--base", "abc123", "--migrations", str(tmp_path), "--pgtap", str(tmp_path)],
        git=broken_git,
    )

    assert status == 2
