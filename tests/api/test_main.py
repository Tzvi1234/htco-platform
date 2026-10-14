"""Process entry point: fail fast on bad configuration, then serve (AC-1.2.5)."""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from htco_api.main import EXIT_CONFIGURATION_ERROR, main
from htco_api.settings import ApiSettings, Environment


class _FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[FastAPI, ApiSettings]] = []

    def __call__(self, app: FastAPI, settings: ApiSettings) -> None:
        self.calls.append((app, settings))


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The process environment without any HTCO_* variable."""
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """`main` configures logging for the whole process; put the test runner's handlers back."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.mark.ac("AC-1.2.5")
@pytest.mark.usefixtures("environment")
def test_main__missing_environment__exits_with_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve = _FakeServer()

    exit_code = main(serve=serve)

    assert exit_code == EXIT_CONFIGURATION_ERROR
    assert "HTCO_ENVIRONMENT is required" in capsys.readouterr().err
    assert serve.calls == []


@pytest.mark.ac("AC-1.2.5")
def test_main__invalid_port__names_the_setting(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_PORT", "70000")

    exit_code = main(serve=_FakeServer())

    assert exit_code == EXIT_CONFIGURATION_ERROR
    assert "HTCO_PORT" in capsys.readouterr().err


@pytest.mark.usefixtures("restore_logging")
def test_main__valid_configuration__logs_json_and_serves_the_app(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_PORT", "8123")
    serve = _FakeServer()

    exit_code = main(serve=serve)

    assert exit_code == 0
    [(app, settings)] = serve.calls
    assert isinstance(app, FastAPI)
    assert (settings.environment, settings.port) == (Environment.LOCAL, 8123)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["api.starting"]
    assert lines[0]["environment"] == "local"


@pytest.mark.ac("AC-1.2.5")
def test_process__started_without_required_setting__exits_non_zero_with_a_clear_message() -> None:
    env = {name: value for name, value in os.environ.items() if not name.startswith("HTCO_")}

    result = subprocess.run(
        [sys.executable, "-m", "htco_api"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == EXIT_CONFIGURATION_ERROR
    assert "HTCO_ENVIRONMENT is required" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.ac("AC-1.7.1")
@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("environment_name", "status"), [("production", 404), ("staging", 500)])
def test_main__diagnostic_endpoint__exists_only_outside_production(
    environment: pytest.MonkeyPatch, environment_name: str, status: int
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", environment_name)
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        assert client.post("/_diagnostics/error").status_code == status
