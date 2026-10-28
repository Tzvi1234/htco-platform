"""Worker settings, read from `HTCO_*` environment variables and validated at startup."""

import os
import socket
from datetime import time
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from htco_application.fulfillment_schedule import (
    FULFILLMENT_SCHEDULE,
    RELAY_RETENTION_SCHEDULE,
    REVIEW_SCHEDULE,
)
from htco_application.maintenance import EXPIRY_SCHEDULE, PROBE_SCHEDULE
from htco_application.maintenance import SCHEDULES as MAINTENANCE_SCHEDULES
from htco_application.order_intake import (
    DEFAULT_RECONCILIATION_TIMES,
    DEFAULT_RECONCILIATION_TZ,
    ORDER_SCHEDULES,
    RECONCILIATION_TICK,
    parse_reconciliation_times,
)
from htco_platform.crypto import KeyRing

# Every schedule a worker may run: maintenance (5.1), order reconciliation (5.5), the
# fulfillment inspection (6.1, 6.2) and the low-review read (6.9).
SCHEDULES = (
    MAINTENANCE_SCHEDULES
    | ORDER_SCHEDULES
    | {FULFILLMENT_SCHEDULE.kind: FULFILLMENT_SCHEDULE, REVIEW_SCHEDULE.kind: REVIEW_SCHEDULE}
    | {RELAY_RETENTION_SCHEDULE.kind: RELAY_RETENTION_SCHEDULE}
)


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTCO_", frozen=True, hide_input_in_errors=True, extra="ignore"
    )

    environment: Literal["local", "test", "staging", "production"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    release: str | None = None
    # Error monitoring (AC-6.7.4: dead-lettered executions) is off without a DSN. A secret.
    sentry_dsn: SecretStr | None = None
    # The same database as the API; the worker's connections switch to the `htco_worker` role.
    database_url: SecretStr
    # The worker's own login, `htco_worker_login` (architecture audit 5.1, ER-SEC-08): the URL
    # without a password, and the password apart. Used only when both are set; until then the
    # worker falls back to HTCO_DATABASE_URL (the API's login) and logs a WARNING at every start.
    worker_database_url: str | None = None
    worker_database_password: SecretStr | None = None
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
    # Slice 5.5: when reconciliation runs, local "HH:MM", comma-separated; the day's last run is
    # the full sweep. Twice a day by default (the owner's decision of 2026-10-24).
    reconciliation_schedule: str = DEFAULT_RECONCILIATION_TIMES
    # The IANA time zone of those times (06:00 stays 06:00 across a DST change).
    reconciliation_tz: str = DEFAULT_RECONCILIATION_TZ
    # The reviewed registry and the action contracts, copied into the image next to the code.
    registry_path: Path = Path("registry")
    action_schemas_path: Path = Path("packages/contracts/actions")
    # Email (slice 6.4): `log` (default) sends nothing and logs the kind and key; `resend` sends
    # through Resend, only once the owner verified office.ht-co.app (SPF, DKIM, DMARC).
    email_provider: Literal["log", "resend"] = "log"
    resend_api_key: SecretStr | None = None
    email_reply_to: str = Field(default="support@ht-co.app", pattern=r"^[^@\s]+@[^@\s]+$")
    office_url: str = Field(default="https://office.ht-co.app", pattern=r"^https://[^\s]+$")

    @field_validator(
        "sentry_dsn",
        "etsy_keystring",
        "etsy_shared_secret",
        "token_vault_key",
        "token_vault_previous_keys",
        "resend_api_key",
        "worker_database_url",
        "worker_database_password",
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
    def reconciliation_times(self) -> tuple[time, ...]:
        return parse_reconciliation_times(self.reconciliation_schedule)

    @property
    def reconciliation_zone(self) -> ZoneInfo:
        return ZoneInfo(self.reconciliation_tz)

    @property
    def key_ring(self) -> KeyRing | None:
        if self.token_vault_key is None:
            return None
        previous = self.token_vault_previous_keys
        return KeyRing.parse(
            self.token_vault_key.get_secret_value(),
            None if previous is None else previous.get_secret_value(),
        )

    @property
    def own_login(self) -> tuple[str, str] | None:
        """The worker's own login (URL, password) when both are configured, else None."""
        if self.worker_database_url is None or self.worker_database_password is None:
            return None
        return self.worker_database_url, self.worker_database_password.get_secret_value()

    @field_validator("reconciliation_schedule")
    @classmethod
    def _reconciliation_times_parse(cls, value: str) -> str:
        try:
            parse_reconciliation_times(value)
        except ValueError as error:
            raise ValueError(f"HTCO_RECONCILIATION_SCHEDULE: {error}") from None
        return value

    @field_validator("reconciliation_tz")
    @classmethod
    def _reconciliation_zone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ValueError, ZoneInfoNotFoundError:
            raise ValueError(f"HTCO_RECONCILIATION_TZ: no time zone {value!r}") from None
        return value

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
        etsy_schedules = {
            PROBE_SCHEDULE.kind,
            EXPIRY_SCHEDULE.kind,
            RECONCILIATION_TICK.kind,
        } & set(self.schedules)
        if etsy_schedules and ring is None:
            raise ValueError(f"{sorted(etsy_schedules)} need HTCO_TOKEN_VAULT_KEY")
        if self.worker_database_password is not None and self.worker_database_url is None:
            raise ValueError("HTCO_WORKER_DATABASE_PASSWORD needs HTCO_WORKER_DATABASE_URL")
        if self.email_provider == "resend" and self.resend_api_key is None:
            raise ValueError("HTCO_EMAIL_PROVIDER=resend needs HTCO_RESEND_API_KEY")
        etsy_app = self.etsy_keystring is not None and self.etsy_shared_secret is not None
        for needs_app in (PROBE_SCHEDULE.kind, RECONCILIATION_TICK.kind):
            if needs_app in self.schedules and not etsy_app:
                raise ValueError(
                    f"{needs_app} needs HTCO_ETSY_KEYSTRING and HTCO_ETSY_SHARED_SECRET"
                )
        return self
