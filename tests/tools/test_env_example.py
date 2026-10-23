"""`.env.example` lists every variable the API and the worker read (ER-OPS: documented flags)."""

import re
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from htco_api.settings import ApiSettings
from htco_worker.settings import WorkerSettings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _listed() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*(HTCO_[A-Z0-9_]+)=", text, flags=re.MULTILINE))


@pytest.mark.parametrize("settings", [ApiSettings, WorkerSettings], ids=["api", "worker"])
def test_env_example__lists_every_setting(settings: type[BaseSettings]) -> None:
    expected = {f"HTCO_{name.upper()}" for name in settings.model_fields}

    missing = sorted(expected - _listed())

    assert missing == [], f".env.example does not list: {missing}"
