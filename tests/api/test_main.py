"""Process entry point: fail fast on bad configuration, then serve (AC-1.2.5)."""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import override

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from htco_api.main import EXIT_CONFIGURATION_ERROR, main
from htco_api.settings import ApiSettings, Environment
from htco_platform.error_reporting import ErrorReporter

# Nothing listens on port 1: startup never connects, and readiness fails fast.
UNREACHABLE_DATABASE_URL = "postgresql://htco:not-a-secret@127.0.0.1:1/postgres"


class _FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[FastAPI, ApiSettings]] = []

    def __call__(self, app: FastAPI, settings: ApiSettings) -> None:
        self.calls.append((app, settings))


class _RecordingReporter(ErrorReporter):
    def __init__(self, events: list[str]) -> None:
        self._events = events

    @override
    def report(self, error: BaseException, *, request_id: str | None, trace_id: str | None) -> None:
        self._events.append("report")

    @override
    def close(self) -> None:
        self._events.append("close")


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
    environment.setenv("HTCO_DATABASE_URL", UNREACHABLE_DATABASE_URL)
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        assert client.post("/_diagnostics/error").status_code == status


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("serve_fails", [False, True])
def test_main__shutdown__closes_the_error_reporter_after_serving(
    environment: pytest.MonkeyPatch, *, serve_fails: bool
) -> None:
    """Reports queued during shutdown are delivered before the process exits."""
    environment.setenv("HTCO_ENVIRONMENT", "local")
    events: list[str] = []

    def serve(_app: FastAPI, _settings: ApiSettings) -> None:
        events.append("serve")
        if serve_fails:
            raise OSError("address already in use")

    def new_reporter(_settings: ApiSettings) -> ErrorReporter:
        return _RecordingReporter(events)

    if serve_fails:
        with pytest.raises(OSError, match="address"):
            main(serve=serve, new_error_reporter=new_reporter)
    else:
        main(serve=serve, new_error_reporter=new_reporter)

    assert events == ["serve", "close"]


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("dsn", ["", "   "])
def test_main__blank_sentry_dsn__leaves_error_monitoring_off(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dsn: str
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_SENTRY_DSN", dsn)

    main(serve=_FakeServer())

    [line] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert line["error_monitoring"] == "off"


@pytest.mark.ac("AC-2.1.3")
@pytest.mark.parametrize("environment_name", ["staging", "production"])
def test_main__deployed_environment_without_database__exits_with_configuration_error(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environment_name: str
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", environment_name)

    exit_code = main(serve=_FakeServer())

    assert exit_code == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL is required in staging and production" in capsys.readouterr().err


@pytest.mark.ac("AC-2.1.3")
@pytest.mark.usefixtures("restore_logging")
def test_main__database_configured__readyz_checks_it(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_DATABASE_URL", UNREACHABLE_DATABASE_URL)
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"configuration": "ok", "database": "failed"}
    assert "not-a-secret" not in capsys.readouterr().out


@pytest.mark.usefixtures("restore_logging")
def test_main__no_database_configured__readyz_has_no_database_check(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"configuration": "ok"}
