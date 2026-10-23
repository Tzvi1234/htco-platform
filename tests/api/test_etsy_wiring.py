"""The composition root and the Etsy connect flow (slices 4.3 and 4.4).

The connect flow is on only with the Etsy app credentials, the redirect URI, the token vault key
and web sign-in. Without any of them the API still starts, the startup log says `off`, and the
connect endpoints answer 503 `etsy-unavailable`. No secret reaches the log.
"""

import base64
import json
import logging
import os
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from htco_api.main import main
from htco_api.settings import ApiSettings
from htco_application.order_intake import QueuedSyncRequests

VAULT_KEY = base64.b64encode(b"V" * 32).decode()
SESSION_KEY = base64.b64encode(b"S" * 32).decode()
KEYSTRING = "fakekeystring"
SHARED_SECRET = "fake-shared-secret"  # noqa: S105 - obviously fake test value
REDIRECT = "https://dash.ht-co.app/integrations/etsy/callback"


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
    for name, value in {
        "HTCO_ENVIRONMENT": "local",
        "HTCO_DATABASE_URL": "postgresql://htco@db.invalid/htco",
        "HTCO_SUPABASE_URL": "http://127.0.0.1:54321",
        "HTCO_SESSION_KEY": SESSION_KEY,
        "HTCO_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fake",
        "HTCO_WEB_ORIGINS": "https://dash.ht-co.app",
        "HTCO_ETSY_KEYSTRING": KEYSTRING,
        "HTCO_ETSY_SHARED_SECRET": SHARED_SECRET,
        "HTCO_ETSY_REDIRECT_URI": REDIRECT,
        "HTCO_TOKEN_VAULT_KEY": f"key-v:{VAULT_KEY}",
    }.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def _starting(output: str) -> dict[str, object]:
    [line] = [json.loads(line) for line in output.splitlines() if '"api.starting"' in line]
    starting: dict[str, object] = line
    return starting


@pytest.mark.ac("AC-4.2.3")
@pytest.mark.usefixtures("restore_logging")
def test_main__everything_configured__etsy_connect_on_and_no_secret_logged(
    environment: pytest.MonkeyPatch,  # noqa: ARG001 - sets the environment
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _Server()

    assert main(serve=server) == 0

    captured = capsys.readouterr()
    assert _starting(captured.out)["etsy_connect"] == "on"
    assert server.apps[0].state.etsy is not None
    for secret in (VAULT_KEY, SESSION_KEY, SHARED_SECRET):
        assert secret not in captured.out + captured.err


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    "missing",
    [
        "HTCO_TOKEN_VAULT_KEY",
        "HTCO_ETSY_REDIRECT_URI",
        ("HTCO_ETSY_KEYSTRING", "HTCO_ETSY_SHARED_SECRET"),
        "HTCO_SESSION_KEY",
    ],
)
def test_main__a_piece_missing__boots_with_etsy_connect_off(
    environment: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str | tuple[str, ...],
) -> None:
    for name in (missing,) if isinstance(missing, str) else missing:
        environment.setenv(name, "")  # a blank secret, as the deploy injects an unset one
    server = _Server()

    assert main(serve=server) == 0

    assert _starting(capsys.readouterr().out)["etsy_connect"] == "off"
    assert server.apps[0].state.etsy is None


@pytest.mark.anyio
@pytest.mark.usefixtures("restore_logging")
async def test_main__etsy_off__connect_still_asks_for_a_session_first(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_TOKEN_VAULT_KEY", "")
    server = _Server()
    main(serve=server)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.apps[0]), base_url="https://api.test"
    ) as http:
        response = await http.post("/v1/shops/etsy/connect")

    assert response.status_code == 401  # no session: refused before anything else


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("flag", "in_api"), [(None, True), ("true", True), ("false", False)])
def test_main__order_sync_runs_in_the_api_only_as_the_fallback(
    environment: pytest.MonkeyPatch, flag: str | None, in_api: bool
) -> None:
    """Slice 5.4: `HTCO_ORDER_SYNC_IN_API` off makes stale reads ask the worker's queue."""
    if flag is not None:
        environment.setenv("HTCO_ORDER_SYNC_IN_API", flag)
    server = _Server()

    assert main(serve=server) == 0

    history = server.apps[0].state.dashboard.history
    assert (history.requests is None) is in_api
    if not in_api:
        assert isinstance(history.requests, QueuedSyncRequests)
