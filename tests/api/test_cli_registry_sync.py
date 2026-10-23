"""`htco registry sync --automations` (slice 5.10, AC-5.10.7): a dry run shows the diff, `--apply`
writes it once with the registry commit, `--check` fails while anything differs."""

import io
import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from htco_api.cli import EXIT_CONFIGURATION_ERROR, EXIT_DATA_ERROR, main
from htco_application.automation.records import DefinitionRow

REGISTRY = Path(__file__).resolve().parents[2] / "registry"
SYNC = ["registry", "sync", "--automations", "--root", str(REGISTRY), "--commit", "abc1234"]


class FakeCatalog:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], DefinitionRow] = {}
        self.writes = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def current(self) -> list[DefinitionRow]:
        latest: dict[str, DefinitionRow] = {}
        for (automation, version), row in sorted(self.rows.items()):
            if automation not in latest or latest[automation].key[1] < version:
                latest[automation] = row
        return list(latest.values())

    def upsert(self, rows: Sequence[Any]) -> None:
        self.writes += 1
        for _, row in rows:
            self.rows[row.key] = row


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


def run(catalog: FakeCatalog, *extra: str, args: list[str] | None = None) -> tuple[int, list[Any]]:
    out = io.StringIO()
    code = main([*(args or SYNC), *extra], new_catalog=lambda _: catalog, stdout=out)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.ac("AC-5.10.7")
@pytest.mark.usefixtures("environment")
def test_sync__dry_run_shows_every_definition_and_writes_nothing() -> None:
    catalog = FakeCatalog()

    code, lines = run(catalog)

    assert code == 0
    *changes, summary = lines
    assert len(changes) == 15
    assert {c["change"] for c in changes} == {"added"}
    assert summary == {"added": 15, "changed": 0, "missing": 0, "unchanged": 0, "applied": False}
    assert catalog.writes == 0


@pytest.mark.ac("AC-5.10.7")
@pytest.mark.usefixtures("environment")
def test_sync__apply_is_idempotent_and_records_the_commit() -> None:
    catalog = FakeCatalog()

    first, _ = run(catalog, "--apply")
    again, lines = run(catalog, "--apply")
    checked, _ = run(catalog, "--check")

    assert (first, again, checked) == (0, 0, 0)
    assert catalog.writes == 1
    assert lines[-1]["unchanged"] == 15
    assert {row.registry_commit for row in catalog.rows.values()} == {"abc1234"}


@pytest.mark.ac("AC-5.10.7")
@pytest.mark.usefixtures("environment")
def test_sync__check_fails_while_the_database_differs() -> None:
    code, _ = run(FakeCatalog(), "--check")

    assert code == 1


@pytest.mark.usefixtures("environment")
def test_sync__an_automation_removed_from_the_registry__blocks_the_sync(tmp_path: Path) -> None:
    catalog = FakeCatalog()
    run(catalog, "--apply")
    root = tmp_path / "registry"
    shutil.copytree(REGISTRY, root)
    (root / "automations" / "email-intake-routing.toml").unlink()
    args = ["registry", "sync", "--automations", "--root", str(root), "--commit", "def5678"]

    code, lines = run(catalog, "--apply", args=args)

    assert code == EXIT_DATA_ERROR
    assert {"change": "missing", "automation_id": "email-intake-routing"}.items() <= lines[
        0
    ].items()
    assert catalog.writes == 1


@pytest.mark.usefixtures("environment")
def test_sync__invalid_registry__exits_65(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    shutil.copytree(REGISTRY, root)
    path = root / "automations" / "tracking-sync.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("version = 1", "version = 0"))
    args = ["registry", "sync", "--automations", "--root", str(root)]

    code, _ = run(FakeCatalog(), args=args)

    assert code == EXIT_DATA_ERROR


def test_sync__without_the_database__exits_78(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("HTCO_DATABASE_URL")

    assert main(SYNC) == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.usefixtures("environment")
def test_sync__commit_from_github_or_git(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = FakeCatalog()
    monkeypatch.setenv("GITHUB_SHA", "0123456789abcdef0123456789abcdef01234567")
    args = ["registry", "sync", "--automations", "--root", str(REGISTRY), "--apply"]

    run(catalog, args=args)

    assert {r.registry_commit for r in catalog.rows.values()} == {
        "0123456789abcdef0123456789abcdef01234567"
    }
