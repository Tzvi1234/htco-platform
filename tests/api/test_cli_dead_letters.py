"""`htco admin dead-letters` (slice 5.1, AC-5.1.3): the dead-lettered jobs, for the operator.

One JSON line per job with its kind, attempts and last error; never a payload.
"""

import io
import json
import os
import uuid
from datetime import UTC, datetime

import pytest

from htco_api.cli import EXIT_CONFIGURATION_ERROR, main
from htco_application.jobs import DeadLetter

ORG = uuid.UUID("0a000000-0000-0000-0000-000000000000")
FAILED = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class FakeReader:
    def __init__(self, found: list[DeadLetter]) -> None:
        self.found = found
        self.asked: list[tuple[uuid.UUID | None, int]] = []
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def dead_letters(
        self, *, organization_id: uuid.UUID | None, limit: int
    ) -> list[DeadLetter]:
        self.asked.append((organization_id, limit))
        return self.found


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


@pytest.mark.ac("AC-5.1.3")
@pytest.mark.usefixtures("environment")
def test_dead_letters__prints_one_line_per_dead_job_with_its_last_error() -> None:
    job_id = uuid.uuid7()
    reader = FakeReader(
        [
            DeadLetter(
                id=job_id,
                kind="etsy.capability_probe",
                organization_id=ORG,
                shop_id=None,
                attempts=3,
                last_error="MarketplaceUnavailableError",
                created_at=FAILED,
                failed_at=FAILED,
            )
        ]
    )
    out = io.StringIO()

    code = main(
        ["admin", "dead-letters", "--organization", str(ORG), "--limit", "5"],
        new_dead_letters=lambda _: reader,
        stdout=out,
    )

    assert code == 0
    assert reader.asked == [(ORG, 5)]
    assert (reader.started, reader.stopped) == (True, True)
    assert json.loads(out.getvalue()) == {
        "id": str(job_id),
        "kind": "etsy.capability_probe",
        "organization_id": str(ORG),
        "shop_id": None,
        "attempts": 3,
        "last_error": "MarketplaceUnavailableError",
        "failed_at": "2026-10-22T09:00:00+00:00",
    }


@pytest.mark.usefixtures("environment")
def test_dead_letters__every_organization_by_default() -> None:
    reader = FakeReader([])

    assert main(["admin", "dead-letters"], new_dead_letters=lambda _: reader) == 0
    assert reader.asked == [(None, 50)]


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("limit", ["0", "501", "many"])
def test_dead_letters__limit_out_of_range__is_a_usage_error(limit: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["admin", "dead-letters", "--limit", limit], new_dead_letters=lambda _: FakeReader([]))

    assert stopped.value.code == 2


def test_dead_letters__without_the_database__exits_78(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("HTCO_DATABASE_URL")

    assert main(["admin", "dead-letters"]) == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL" in capsys.readouterr().err
