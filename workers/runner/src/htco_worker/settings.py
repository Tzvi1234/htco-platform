"""Worker settings, read from `HTCO_*` environment variables and validated at startup."""

import os
import socket
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_application.maintenance import EXPIRY_SCHEDULE, PROBE_SCHEDULE, SCHEDULES
from htco_platform.crypto import KeyRing


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTCO_", frozen=True, hide_input_in_errors=True, extra="ignore"
    )

    environment: Literal["local", "test", "staging", "production"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    release: str | None = None
    # The same database as the API; the worker's connections switch to the `htco_worker` role.
    database_url: SecretStr
    # Defaults to the host name and process ID, unique per running process.
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}"[:200],
        min_length=1,
        max_length=200,  # the queue's columns hold 200 characters
    )
    # Off by default: each maintenance schedule runs only when named here (comma-separated), so
    # a worker deployed without it changes nothing (feature flag, ER-OPS-04).
    worker_schedules: str = ""
    worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    # App Platform waits a little after SIGTERM before it kills the container.
    worker_shutdown_grace_seconds: float = Field(default=20.0, ge=0, le=120)
    # 0 turns the health port off (local runs).
    worker_health_port: int = Field(default=8081, ge=0, le=65535)
    # The Etsy app and the token vault, for the daily probes and the expiry check.
    etsy_keystring: SecretStr | None = None
    etsy_shared_secret: SecretStr | None = None
    etsy_daily_quota_floor: int = Field(default=500, ge=0)
    token_vault_key: SecretStr | None = None
    token_vault_previous_keys: SecretStr | None = None
    # The Action Gateway (slice 5.4): `reads` (default) runs Etsy reads through the real gateway,
    # writes refused until approvals exist (5.14); `off` switches back to refusing every action.
    worker_action_gateway: Literal["reads", "off"] = "reads"
    # The reviewed registry and the action contracts, copied into the image next to the code.
    registry_path: Path = Path("registry")
    action_schemas_path: Path = Path("packages/contracts/actions")

    @field_validator(
        "etsy_keystring",
        "etsy_shared_secret",
        "token_vault_key",
        "token_vault_previous_keys",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def schedules(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.worker_schedules.split(",") if name.strip())

    @property
    def key_ring(self) -> KeyRing | None:
        if self.token_vault_key is None:
            return None
        previous = self.token_vault_previous_keys
        return KeyRing.parse(
            self.token_vault_key.get_secret_value(),
            None if previous is None else previous.get_secret_value(),
        )

    @model_validator(mode="after")
    def _schedules_are_known_and_configured(self) -> Self:
        unknown = sorted(set(self.schedules) - SCHEDULES.keys())
        if unknown:
            raise ValueError(f"HTCO_WORKER_SCHEDULES names unknown schedules: {unknown}")
        try:
            ring = self.key_ring
        except ValueError:
            raise ValueError(
                "HTCO_TOKEN_VAULT_KEY or HTCO_TOKEN_VAULT_PREVIOUS_KEYS is invalid"
            ) from None
        etsy_schedules = {PROBE_SCHEDULE.kind, EXPIRY_SCHEDULE.kind} & set(self.schedules)
        if etsy_schedules and ring is None:
            raise ValueError(f"{sorted(etsy_schedules)} need HTCO_TOKEN_VAULT_KEY")
        etsy_app = self.etsy_keystring is not None and self.etsy_shared_secret is not None
        if PROBE_SCHEDULE.kind in self.schedules and not etsy_app:
            raise ValueError(
                f"{PROBE_SCHEDULE.kind} needs HTCO_ETSY_KEYSTRING and HTCO_ETSY_SHARED_SECRET"
            )
        return self
