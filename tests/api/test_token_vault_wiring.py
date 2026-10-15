"""The composition root and the Etsy token vault (slice 4.2).

Without the vault key the API still starts, with the Etsy integration off; with it, the startup
log says the vault is on and never shows a key.
"""

import base64
import json
import logging
import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from htco_api.main import main
from htco_api.settings import ApiSettings

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()


class _Server:
    def __init__(self) -> None:
        self.apps: list[FastAPI] = []

    def __call__(self, app: FastAPI, _settings: ApiSettings) -> None:
        self.apps.append(app)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_ENVIRONMENT", "local")
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    monkeypatch.setenv("HTCO_SUPABASE_URL", "http://127.0.0.1:54321")
    return monkeypatch


def _starting(output: str) -> dict[str, object]:
    [line] = [json.loads(line) for line in output.splitlines() if '"api.starting"' in line]
    starting: dict[str, object] = line
    return starting


@pytest.mark.ac("AC-4.2.3")
@pytest.mark.usefixtures("restore_logging")
def test_main__vault_keys__turn_the_vault_on_and_never_reach_the_log(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("HTCO_TOKEN_VAULT_KEY", f"key-b:{KEY_B}")
    environment.setenv("HTCO_TOKEN_VAULT_PREVIOUS_KEYS", f"key-a:{KEY_A}")

    assert main(serve=_Server()) == 0

    captured = capsys.readouterr()
    assert _starting(captured.out)["etsy_token_vault"] == "on"  # noqa: S105 - a status
    for secret in (KEY_A, KEY_B):
        assert secret not in captured.out
        assert secret not in captured.err


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("blank", [None, ""])
def test_main__without_a_vault_key__boots_with_the_vault_off(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], blank: str | None
) -> None:
    if blank is not None:
        environment.setenv("HTCO_TOKEN_VAULT_KEY", blank)
        environment.setenv("HTCO_TOKEN_VAULT_PREVIOUS_KEYS", blank)
    server = _Server()

    assert main(serve=server) == 0

    assert len(server.apps) == 1
    assert _starting(capsys.readouterr().out)["etsy_token_vault"] == "off"  # noqa: S105 - a status


@pytest.mark.ac("AC-4.2.3")
@pytest.mark.usefixtures("restore_logging")
def test_main__invalid_vault_key__exits_naming_the_setting_not_the_value(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    short = base64.b64encode(b"short-key-material").decode()
    environment.setenv("HTCO_TOKEN_VAULT_KEY", f"key-a:{short}")

    assert main(serve=_Server()) == 78

    captured = capsys.readouterr()
    assert "HTCO_TOKEN_VAULT_KEY" in captured.err
    assert short not in captured.err + captured.out
