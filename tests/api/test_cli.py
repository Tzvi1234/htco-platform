"""`htco admin bootstrap` (slice 2.5): the command around the bootstrap use case."""

import io
import json
import os
import uuid
from collections.abc import Callable

import pytest

from htco_api.cli import EXIT_CONFIGURATION_ERROR, EXIT_DATA_ERROR, main
from htco_application.audit import AuditRecord
from htco_application.bootstrap import BootstrapUnitOfWork
from htco_domain.identity import Role

USER = uuid.UUID("11111111-0000-0000-0000-000000000001")


class FakeStore:
    """Knows one user; remembers the owner membership it creates."""

    def __init__(self) -> None:
        self.started = self.stopped = False
        self.owner: tuple[uuid.UUID, uuid.UUID] | None = None
        self.audit: list[AuditRecord] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def run[T](self, work: Callable[[BootstrapUnitOfWork], T]) -> T:
        return work(self)

    def find_user_id(self, email: str) -> uuid.UUID | None:
        return USER if email == "owner@example.test" else None

    def owner_membership_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None:
        return self.owner if user_id == USER else None

    def create_organization(self, organization_id: uuid.UUID, name: str) -> None:
        self.organization = (organization_id, name)

    def ensure_profile(self, user_id: uuid.UUID, display_name: str) -> None:
        self.profile = (user_id, display_name)

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None:
        assert (user_id, role) == (USER, Role.OWNER)
        self.owner = (organization_id, membership_id)

    def append_audit(self, record: AuditRecord) -> None:
        self.audit.append(record)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    return monkeypatch


ARGS = [
    "admin",
    "bootstrap",
    "--email",
    "owner@example.test",
    "--organization",
    "HT Commerce",
    "--display-name",
    "Tzvi",
]


@pytest.mark.usefixtures("environment")
def test_bootstrap__creates_then_finds_and_prints_json() -> None:
    store = FakeStore()
    out = io.StringIO()

    assert main(ARGS, new_store=lambda _url: store, stdout=out) == 0
    assert main(ARGS, new_store=lambda _url: store, stdout=out) == 0

    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert first["created"] is True
    assert second["created"] is False
    assert first["organization_id"] == second["organization_id"]
    assert store.started
    assert store.stopped
    assert len(store.audit) == 2
    assert len({record.correlation_id for record in store.audit}) == 1


@pytest.mark.usefixtures("environment")
def test_bootstrap__unknown_email__data_error_with_a_clear_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = [*ARGS]
    args[3] = "someone@example.test"
    store = FakeStore()

    assert main(args, new_store=lambda _url: store) == EXIT_DATA_ERROR
    assert "sign up" in capsys.readouterr().err
    assert store.stopped


@pytest.mark.usefixtures("environment")
def test_bootstrap__invalid_email__data_error(capsys: pytest.CaptureFixture[str]) -> None:
    args = [*ARGS]
    args[3] = "not-an-email"
    assert main(args, new_store=lambda _url: FakeStore()) == EXIT_DATA_ERROR
    assert "email" in capsys.readouterr().err


def test_bootstrap__without_a_database_url__configuration_error(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("HTCO_DATABASE_URL")
    assert main(ARGS, new_store=lambda _url: FakeStore()) == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.usefixtures("environment")
def test_bootstrap__store_gets_the_database_url() -> None:
    urls: list[str] = []

    def new_store(url: str) -> FakeStore:
        urls.append(url)
        return FakeStore()

    main(ARGS, new_store=new_store, stdout=io.StringIO())
    assert urls == ["postgresql://htco@db.invalid/htco"]


@pytest.mark.parametrize("argv", [[], ["admin"], ["admin", "bootstrap"], ["unknown"]])
def test_usage_errors__exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv, new_store=lambda _url: FakeStore())
    assert caught.value.code == 2
