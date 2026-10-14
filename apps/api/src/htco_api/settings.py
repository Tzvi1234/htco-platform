"""API settings, read from `HTCO_*` environment variables."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ApiSettings(BaseSettings):
    """Validated once at startup; a missing or invalid value stops the process (ER-CODE-07)."""

    model_config = SettingsConfigDict(env_prefix="HTCO_", frozen=True)

    environment: Environment
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    # Error monitoring is off without a DSN. The DSN is a secret: it never reaches logs.
    sentry_dsn: SecretStr | None = None
    # The deployed build (a git commit SHA), attached to error reports.
    release: str | None = None

    @field_validator("sentry_dsn", mode="before")
    @classmethod
    def _blank_dsn_is_none(cls, value: object) -> object:
        """An empty `HTCO_SENTRY_DSN` means "off", never a reporter that silently drops events."""
        if isinstance(value, str) and not value.strip():
            return None
        return value
