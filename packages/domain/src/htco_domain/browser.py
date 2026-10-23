"""Shop browser environments (ADR-0020, `execution-engine.md` §7, slice 5.16).

A shop may have one optional browser environment: a persistent, honest browser profile that the
Browser channel uses for work Etsy's API cannot do. Its lifecycle is the seven statuses below
and the transition table of §7; anything else is refused here and again by the database. Only a
`READY` environment accepts browser actions.

The record is honest by design (ADR-0020): it has no fingerprint, user-agent or proxy fields,
because the browser keeps its stock fingerprint and every shop uses the platform's one egress.
A shop without a row is `NOT_CONFIGURED`.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from htco_domain.actors import ActorRef
from htco_domain.state_machine import check_transition

MAX_REASON = 500
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A health problem is a stable code ("egress_ip_mismatch"), never free text that could carry an
# address, a URL or page content.
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class EnvironmentStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    PROVISIONING = "provisioning"
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    FAILED = "failed"


_E = EnvironmentStatus
_ENVIRONMENT_TRANSITIONS: Mapping[EnvironmentStatus, frozenset[EnvironmentStatus]] = {
    # The Owner or an Admin creates it (step-up, consent).
    _E.NOT_CONFIGURED: frozenset({_E.PROVISIONING}),
    # Ready; credentials missing or a login needs a person; or failed after its retries. Every
    # configured status can also be deleted, back to NOT_CONFIGURED (audited).
    _E.PROVISIONING: frozenset({_E.READY, _E.LOGIN_REQUIRED, _E.FAILED, _E.NOT_CONFIGURED}),
    _E.READY: frozenset({_E.LOGIN_REQUIRED, _E.DEGRADED, _E.SUSPENDED, _E.NOT_CONFIGURED}),
    # A person completed the login; or suspended (kill switch, two challenges in 24 h).
    _E.LOGIN_REQUIRED: frozenset({_E.READY, _E.SUSPENDED, _E.NOT_CONFIGURED}),
    _E.DEGRADED: frozenset({_E.READY, _E.FAILED, _E.SUSPENDED, _E.NOT_CONFIGURED}),
    # The Owner resumes after review.
    _E.SUSPENDED: frozenset({_E.READY, _E.NOT_CONFIGURED}),
    # The Owner re-provisions.
    _E.FAILED: frozenset({_E.PROVISIONING, _E.NOT_CONFIGURED}),
}


def check_environment_transition(current: EnvironmentStatus, target: EnvironmentStatus) -> None:
    check_transition("browser environment", _ENVIRONMENT_TRANSITIONS, current, target)


def _aware(at: datetime) -> bool:
    return at.tzinfo is not None and at.utcoffset() is not None


@dataclass(frozen=True)
class ConsentRecord:
    """The account owner's written consent, in the product (ADR-0020 §6, ADR-0021)."""

    text_version: str  # the version of the consent text that was shown
    user_id: uuid.UUID
    at: datetime
    ip_hash: str  # SHA-256 of the address, never the address

    def __post_init__(self) -> None:
        if not self.text_version.strip() or len(self.text_version) > 64:  # noqa: PLR2004
            raise ValueError("a consent names the text version it was given for")
        if not _SHA256.match(self.ip_hash):
            raise ValueError("a consent keeps a SHA-256 of the address only")
        if not _aware(self.at):
            raise ValueError("a consent's time is in UTC")


@dataclass(frozen=True)
class HealthReport:
    """The result of a preflight or health check (Phase 8A runs them)."""

    ok: bool
    checked_at: datetime
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _aware(self.checked_at):
            raise ValueError("a health report's time is in UTC")
        if self.ok and self.problems:
            raise ValueError("a passing health report has no problems")
        for problem in self.problems:
            if not _CODE.match(problem):
                raise ValueError("a health problem is a short code, never free text")


@dataclass(frozen=True)
class BrowserEnvironment:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    status: EnvironmentStatus
    consent: ConsentRecord | None = None
    challenge_count: int = 0
    last_health_at: datetime | None = None
    last_health_ok: bool | None = None

    def transition(self, target: EnvironmentStatus) -> BrowserEnvironment:
        check_environment_transition(self.status, target)
        return replace(self, status=target)

    @property
    def accepts_browser_actions(self) -> bool:
        return self.status is EnvironmentStatus.READY


@dataclass(frozen=True)
class EnvironmentChange:
    """One recorded transition: who moved the environment, why, and in which flow."""

    environment_id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    from_status: EnvironmentStatus
    to_status: EnvironmentStatus
    actor: ActorRef
    reason: str  # a code or a short text; never a secret or page content
    correlation_id: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.reason.strip() or not self.correlation_id.strip():
            raise ValueError("a change needs a reason and a correlation ID")
        if len(self.reason) > MAX_REASON or len(self.correlation_id) > 200:  # noqa: PLR2004
            raise ValueError(f"a change's reason is at most {MAX_REASON} characters")
        if not _aware(self.occurred_at):
            raise ValueError("a change's time is in UTC")
        check_environment_transition(self.from_status, self.to_status)

    @classmethod
    def of(  # noqa: PLR0913 - one argument per recorded column
        cls,
        before: BrowserEnvironment,
        target: EnvironmentStatus,
        *,
        by: ActorRef,
        reason: str,
        correlation_id: str,
        at: datetime,
    ) -> EnvironmentChange:
        return cls(
            environment_id=before.id, organization_id=before.organization_id,
            shop_id=before.shop_id, from_status=before.status, to_status=target, actor=by,
            reason=reason, correlation_id=correlation_id, occurred_at=at,
        )  # fmt: skip

    @classmethod
    def creation(
        cls,
        created: BrowserEnvironment,
        *,
        by: ActorRef,
        reason: str,
        correlation_id: str,
        at: datetime,
    ) -> EnvironmentChange:
        """A shop without an environment is NOT_CONFIGURED: creating one is that edge."""
        return cls.of(
            replace(created, status=EnvironmentStatus.NOT_CONFIGURED),
            created.status,
            by=by,
            reason=reason,
            correlation_id=correlation_id,
            at=at,
        )


def environment_edges() -> frozenset[tuple[EnvironmentStatus, EnvironmentStatus]]:
    """Every allowed `(from, to)` pair, for the database's mirror of the table."""
    return frozenset(
        (current, target)
        for current, targets in _ENVIRONMENT_TRANSITIONS.items()
        for target in targets
    )
