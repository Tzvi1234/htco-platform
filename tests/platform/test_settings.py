"""Settings load once from the environment and fail fast with clear messages (ER-CODE-07)."""

import pytest
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_platform.settings import ConfigurationError, load_settings


class _ExampleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTCO_EXAMPLE_", frozen=True)

    name: str
    port: int = Field(default=8000, ge=1, le=65535)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("HTCO_EXAMPLE_NAME", "HTCO_EXAMPLE_PORT"):
        monkeypatch.delenv(variable, raising=False)


def test_load_settings__valid_environment__returns_typed_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTCO_EXAMPLE_NAME", "api")
    monkeypatch.setenv("HTCO_EXAMPLE_PORT", "8080")

    settings = load_settings(_ExampleSettings)

    assert (settings.name, settings.port) == ("api", 8080)


@pytest.mark.ac("AC-1.2.5")
def test_load_settings__missing_required_setting__names_the_variable() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(_ExampleSettings)

    assert raised.value.problems == ["HTCO_EXAMPLE_NAME is required"]


@pytest.mark.ac("AC-1.2.5")
def test_load_settings__invalid_value__names_the_variable_without_echoing_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTCO_EXAMPLE_NAME", "api")
    monkeypatch.setenv("HTCO_EXAMPLE_PORT", "sk_live_do-not-print-me")

    with pytest.raises(ConfigurationError) as raised:
        load_settings(_ExampleSettings)

    message = str(raised.value)
    assert "HTCO_EXAMPLE_PORT" in message
    assert "do-not-print-me" not in message


def test_load_settings__several_problems__reports_all_of_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTCO_EXAMPLE_PORT", "0")

    with pytest.raises(ConfigurationError) as raised:
        load_settings(_ExampleSettings)

    assert [problem.split()[0] for problem in raised.value.problems] == [
        "HTCO_EXAMPLE_NAME",
        "HTCO_EXAMPLE_PORT",
    ]


def test_configuration_error__message__lists_one_problem_per_line() -> None:
    error = ConfigurationError(["HTCO_A is required", "HTCO_B must be a number"])

    assert str(error) == "HTCO_A is required\nHTCO_B must be a number"


class _CrossFieldSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HTCO_TEST_")

    mode: str = "local"
    url: str | None = None

    @model_validator(mode="after")
    def _deployed_needs_url(self) -> _CrossFieldSettings:
        if self.mode == "deployed" and self.url is None:
            raise ValueError("HTCO_TEST_URL is required when deployed")
        return self


def test_load_settings__cross_field_rule__reports_its_own_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTCO_TEST_MODE", "deployed")

    with pytest.raises(ConfigurationError) as raised:
        load_settings(_CrossFieldSettings)

    assert raised.value.problems == ["HTCO_TEST_URL is required when deployed"]
