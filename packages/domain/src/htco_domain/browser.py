"""Shop browser environments (ADR-0019, `execution-engine.md` §7).

The seven statuses of a shop's browser environment. The Execution Router (slice 5.13) only
routes a browser action to a `READY` environment. The table, its transition rules and its
registry come with slice 5.16.
"""

from enum import StrEnum


class EnvironmentStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    PROVISIONING = "provisioning"
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    FAILED = "failed"
