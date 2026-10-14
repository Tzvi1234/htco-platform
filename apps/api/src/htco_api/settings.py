"""API settings, read from `HTCO_*` environment variables."""

import base64
import binascii
from enum import StrEnum
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    # Postgres connection string (a secret). Optional locally; required when deployed.
    database_url: SecretStr | None = None
    # The Supabase project (`https://<ref>.supabase.co`; locally `http://127.0.0.1:54321`). Its
    # sign-in service issues the access tokens the API verifies. Required when deployed.
    supabase_url: AnyHttpUrl | None = None
    # Supabase secret key (`sb_secret_…`), used only to send invitations (slice 2.6), never for
    # database queries. Without it, invitations answer 503.
    supabase_secret_key: SecretStr | None = None
    # Where the invitation email's link lands (the web app's welcome page).
    invite_redirect_url: AnyHttpUrl | None = None
    # Web sign-in (slice 3.5). The session key seals Supabase tokens at rest: 32 random bytes,
    # base64 (`openssl rand -base64 32`), a secret. The publishable key identifies the Supabase
    # project to its sign-in service and is not secret. The web origins are the sites allowed to
    # send cookie requests, comma-separated. Without the two keys, web sign-in answers 503.
    session_key: SecretStr | None = None
    supabase_publishable_key: str | None = None
    web_origins: str = ""

    @property
    def web_origin_list(self) -> tuple[str, ...]:
        return tuple(origin.strip() for origin in self.web_origins.split(",") if origin.strip())

    @property
    def auth_issuer(self) -> str | None:
        if self.supabase_url is None:
            return None
        return str(self.supabase_url).rstrip("/") + "/auth/v1"

    @property
    def auth_jwks_url(self) -> str | None:
        issuer = self.auth_issuer
        return None if issuer is None else issuer + "/.well-known/jwks.json"

    @field_validator(
        "sentry_dsn",
        "database_url",
        "supabase_url",
        "supabase_secret_key",
        "invite_redirect_url",
        "session_key",
        "supabase_publishable_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """A blank optional setting is unset: an empty `HTCO_SENTRY_DSN` means "off", and an empty
        injected secret fails the deployed-environment checks instead of passing them."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("session_key")
    @classmethod
    def _session_key_is_32_bytes(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                raw = base64.b64decode(value.get_secret_value(), validate=True)
            except binascii.Error, ValueError:
                raw = b""
            if len(raw) != 32:  # noqa: PLR2004 - AES-256 key size
                raise ValueError("HTCO_SESSION_KEY must be 32 random bytes, base64-encoded")
        return value

    @model_validator(mode="after")
    def _deployed_needs_a_database(self) -> Self:
        if self.environment in {Environment.STAGING, Environment.PRODUCTION} and (
            self.database_url is None
        ):
            raise ValueError("HTCO_DATABASE_URL is required in staging and production")
        return self

    @model_validator(mode="after")
    def _deployed_needs_sign_in(self) -> Self:
        if self.environment not in {Environment.STAGING, Environment.PRODUCTION}:
            return self
        if self.supabase_url is None:
            raise ValueError("HTCO_SUPABASE_URL is required in staging and production")
        if self.supabase_url.scheme != "https":
            raise ValueError("HTCO_SUPABASE_URL must use HTTPS in staging and production")
        return self
