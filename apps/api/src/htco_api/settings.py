"""API settings, read from `HTCO_*` environment variables."""

import base64
import binascii
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_platform.crypto import KeyRing


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ApiSettings(BaseSettings):
    """Validated once at startup; a missing or invalid value stops the process (ER-CODE-07)."""

    # Errors never repeat an input: settings hold secrets (also when an error is printed raw).
    model_config = SettingsConfigDict(env_prefix="HTCO_", frozen=True, hide_input_in_errors=True)

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
    # Where the one-time invitation link lands (the web app's accept-invitation page).
    invite_redirect_url: AnyHttpUrl | None = None
    # Where the emailed password reset link lands (the web app's reset page). Without it, asking
    # for a reset link answers 503.
    password_reset_redirect_url: AnyHttpUrl | None = None
    # Web sign-in (slice 3.5). The session key seals Supabase tokens at rest: 32 random bytes,
    # base64 (`openssl rand -base64 32`), a secret. The publishable key identifies the Supabase
    # project to its sign-in service and is not secret. The web origins are the sites allowed to
    # send cookie requests, comma-separated. Without the two keys, web sign-in answers 503.
    session_key: SecretStr | None = None
    supabase_publishable_key: str | None = None
    web_origins: str = ""
    # Etsy app credentials (slice 4.1, ADR-0008): the keystring and shared secret, sent together
    # as `x-api-key`. Both are secrets and both or neither are set; they become required when
    # Phase 4 connects a shop. Below the daily floor of requests left, routine Etsy calls wait.
    etsy_keystring: SecretStr | None = None
    etsy_shared_secret: SecretStr | None = None
    etsy_daily_quota_floor: int = Field(default=500, ge=0)
    # Where Etsy sends the owner back after consent (slice 4.3): the web app's callback route,
    # registered exactly in the Etsy app. Not secret. HTTPS without query or fragment when
    # deployed. With the credentials, the vault key and web sign-in it turns the connect flow on.
    etsy_redirect_uri: str | None = None
    # The Etsy token vault (slice 4.2). Each key is `<key ID>:<32 random bytes, base64>`, a secret.
    # The current key wraps new data keys; previous keys (comma-separated) only open rows not yet
    # re-wrapped by `htco admin rotate-token-keys`. Without the current key the Etsy integration is
    # off and the API still starts.
    token_vault_key: SecretStr | None = None
    token_vault_previous_keys: SecretStr | None = None
    # Slice 5.10: user triggers and admin retries of automations (`POST /v1/automations/...`).
    # Off by default, so executions never pile up while no agent-worker runs (ER-OPS-04).
    automations_enabled: bool = False

    @property
    def token_key_ring(self) -> KeyRing | None:
        return token_key_ring(self.token_vault_key, self.token_vault_previous_keys)

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
        "password_reset_redirect_url",
        "session_key",
        "supabase_publishable_key",
        "etsy_keystring",
        "etsy_shared_secret",
        "etsy_redirect_uri",
        "token_vault_key",
        "token_vault_previous_keys",
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
    def _etsy_credentials_come_in_pairs(self) -> Self:
        if (self.etsy_keystring is None) != (self.etsy_shared_secret is None):
            raise ValueError("HTCO_ETSY_KEYSTRING and HTCO_ETSY_SHARED_SECRET must be set together")
        return self

    @field_validator("etsy_redirect_uri")
    @classmethod
    def _redirect_uri_is_a_plain_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("HTCO_ETSY_REDIRECT_URI must be an absolute http(s) URL")
        if parts.query or parts.fragment or "#" in value:
            raise ValueError("HTCO_ETSY_REDIRECT_URI must not have a query or a fragment")
        return value.strip()

    @model_validator(mode="after")
    def _deployed_redirect_uri_uses_https(self) -> Self:
        deployed = self.environment in {Environment.STAGING, Environment.PRODUCTION}
        if deployed and self.etsy_redirect_uri and not self.etsy_redirect_uri.startswith("https:"):
            raise ValueError("HTCO_ETSY_REDIRECT_URI must use HTTPS in staging and production")
        return self

    @model_validator(mode="after")
    def _deployed_reset_redirect_uses_https(self) -> Self:
        # The recovery tokens ride in the URL fragment: an HTTP page could be replaced on path.
        deployed = self.environment in {Environment.STAGING, Environment.PRODUCTION}
        url = self.password_reset_redirect_url
        if deployed and url is not None and url.scheme != "https":
            raise ValueError(
                "HTCO_PASSWORD_RESET_REDIRECT_URL must use HTTPS in staging and production"
            )
        return self

    @model_validator(mode="after")
    def _token_vault_keys_are_valid(self) -> Self:
        check_token_vault_keys(self.token_vault_key, self.token_vault_previous_keys)
        return self

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


def token_key_ring(current: SecretStr | None, previous: SecretStr | None) -> KeyRing | None:
    """The vault's key ring, or None when no current key is set (the integration is off)."""
    if current is None:
        return None
    return KeyRing.parse(
        current.get_secret_value(), None if previous is None else previous.get_secret_value()
    )


def check_token_vault_keys(current: SecretStr | None, previous: SecretStr | None) -> None:
    """Raise a `ValueError` naming the setting, never its value."""
    if current is None:
        if previous is not None:
            raise ValueError("HTCO_TOKEN_VAULT_PREVIOUS_KEYS needs HTCO_TOKEN_VAULT_KEY")
        return
    try:
        token_key_ring(current, previous)
    except ValueError as error:
        problem = str(error)  # describes the format or a duplicate key ID, never a key
        raise ValueError(
            f"HTCO_TOKEN_VAULT_KEY and HTCO_TOKEN_VAULT_PREVIOUS_KEYS are invalid: {problem}"
        ) from None
