"""Process entry point: fail fast on bad configuration, then serve (AC-1.2.5)."""

import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from typing import override

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from htco_api.auth import Authenticator
from htco_api.main import EXIT_CONFIGURATION_ERROR, main
from htco_api.settings import ApiSettings, Environment
from htco_platform.error_reporting import ErrorReporter


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
@pytest.mark.slow  # a real interpreter start: 13 to 17 s on a Windows PC (#115)
def test_process__started_without_required_setting__exits_non_zero_with_a_clear_message() -> None:
    env = {name: value for name, value in os.environ.items() if not name.startswith("HTCO_")}

    result = subprocess.run(
        [sys.executable, "-m", "htco_api"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,  # generous: a loaded local machine took more than 60 s once (#115)
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
    environment.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    environment.setenv("HTCO_SUPABASE_URL", "https://project.supabase.invalid")
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


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("environment_name", ["staging", "production"])
def test_main__deployed_without_database_url__exits_with_configuration_error(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environment_name: str
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", environment_name)
    serve = _FakeServer()

    assert main(serve=serve) == EXIT_CONFIGURATION_ERROR
    assert "HTCO_DATABASE_URL" in capsys.readouterr().err
    assert serve.calls == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("environment_name", ["staging", "production"])
def test_main__deployed_without_supabase_url__exits_with_configuration_error(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environment_name: str
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", environment_name)
    environment.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    serve = _FakeServer()

    assert main(serve=serve) == EXIT_CONFIGURATION_ERROR
    assert "HTCO_SUPABASE_URL" in capsys.readouterr().err
    assert serve.calls == []


@pytest.mark.usefixtures("restore_logging")
def test_main__supabase_and_database__authenticates_requests(
    environment: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = int(probe.getsockname()[1])
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_DATABASE_URL", f"postgresql://htco@127.0.0.1:{closed_port}/htco")
    environment.setenv("HTCO_SUPABASE_URL", "http://127.0.0.1:54321")
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    assert isinstance(app.state.authenticator, Authenticator)
    with TestClient(app):  # the database pool and the HTTP client start and stop with the app
        pass


@pytest.mark.usefixtures("restore_logging")
def test_main__without_supabase_locally__has_no_authenticator(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    assert app.state.authenticator is None


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.ac("AC-2.1.3")
def test_main__database_url__readyz_checks_the_database(
    environment: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = int(probe.getsockname()[1])
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_DATABASE_URL", f"postgresql://htco@127.0.0.1:{closed_port}/htco")
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"


@pytest.mark.usefixtures("restore_logging")
def test_main__no_database_url_locally__readyz_checks_configuration_only(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    with TestClient(app) as client:
        assert client.get("/readyz").json()["checks"] == {"configuration": "ok"}
