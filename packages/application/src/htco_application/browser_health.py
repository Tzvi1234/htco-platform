"""Preflight, health status and session validation (slice 8A.3, ADR-0020 §4 and §5).

- `Preflight` is the Browser Controller's check before a session starts (after the profile
  lock, before any engine). The worker's egress IP must equal the configured one, so a
  misconfiguration is caught rather than used (ADR-0020 §4); the disk must have space; every
  target host must resolve and complete a TLS handshake. Problems are codes. A failed preflight
  stops the session (AC-8A.3.1).
- `EnvironmentHealth` records every health report in the environment's history and applies
  `htco_domain.browser.next_health_status`: two failures in a row move `READY` to `DEGRADED`,
  two passes move it back (AC-8A.3.3).
- `SessionValidation.signed_out` is called when a site's signed-in marker is missing: the
  environment moves to `LOGIN_REQUIRED` and one Human task "Sign in to {shop}" opens
  (AC-8A.3.2). A degraded environment keeps its status (the table has no edge to
  `LOGIN_REQUIRED` from it) and still gets the task. `SignInTasks` is the port; its adapter is
  the Phase 7 task store (or the 6.10 relay item until then). A suspended or failed
  environment is left to a person.

Egress is diagnosed, never changed: nothing here can pick another address (ADR-0020 §4).
"""

import ipaddress
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from htco_application.browser_environments import ShopEnvironmentRegistry
from htco_domain.actors import ActorRef
from htco_domain.browser import (
    HEALTH_WINDOW,
    BrowserEnvironment,
    EnvironmentStatus,
    HealthReport,
    next_health_status,
)

EGRESS_MISMATCH = "egress_ip_mismatch"
EGRESS_UNKNOWN = "egress_ip_unknown"
DISK_LOW = "disk_low"
TARGET_UNREACHABLE = "target_unreachable"
SIGNED_OUT = "signed_out"
SIGN_IN_TITLE = "Sign in to {shop}"
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_REASONS = {
    EnvironmentStatus.DEGRADED: "health_failed_twice",
    EnvironmentStatus.READY: "health_passed_twice",
}
# The transition table (`execution-engine.md` §7) has no DEGRADED → LOGIN_REQUIRED edge: a
# degraded environment keeps its status and still gets the sign-in task.
_SIGN_IN_FROM = frozenset({EnvironmentStatus.READY, EnvironmentStatus.PROVISIONING})
_TASK_ONLY = frozenset({EnvironmentStatus.LOGIN_REQUIRED, EnvironmentStatus.DEGRADED})


# --- Preflight ----------------------------------------------------------------------------------


class EgressProbe(Protocol):
    async def current_ip(self) -> str | None:
        """The address the worker's traffic leaves from, or None when it cannot be learned."""
        ...


class DiskProbe(Protocol):
    def free_bytes(self) -> int:
        """Free space where profiles are unpacked (the tmpfs root)."""
        ...


class TargetProbe(Protocol):
    async def reachable_over_tls(self, host: str) -> bool:
        """DNS resolves and a TLS handshake with a valid certificate completes."""
        ...


@dataclass(frozen=True)
class PreflightConfig:
    expected_egress_ip: str
    min_free_bytes: int
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            ipaddress.ip_address(self.expected_egress_ip)
        except ValueError:
            raise ValueError("the expected egress is one IP address") from None
        if self.min_free_bytes < 0:
            raise ValueError("the minimum free space is not negative")
        for host in self.targets:
            if not _HOST.match(host):
                raise ValueError("a preflight target is a host name, never a URL")


class Preflight:
    def __init__(
        self,
        config: PreflightConfig,
        egress: EgressProbe,
        disk: DiskProbe,
        targets: TargetProbe,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._egress = egress
        self._disk = disk
        self._targets = targets
        self._clock = clock

    async def run(self) -> HealthReport:
        problems: list[str] = []
        observed = await self._egress.current_ip()
        problem = _egress_problem(observed, self._config.expected_egress_ip)
        if problem is not None:
            problems.append(problem)
        if self._disk.free_bytes() < self._config.min_free_bytes:
            problems.append(DISK_LOW)
        for host in self._config.targets:
            if not await self._targets.reachable_over_tls(host):
                problems.append(TARGET_UNREACHABLE)
                break
        return HealthReport(ok=not problems, checked_at=self._clock(), problems=tuple(problems))


def _egress_problem(observed: str | None, expected: str) -> str | None:
    if observed is None:
        return EGRESS_UNKNOWN
    try:
        address = ipaddress.ip_address(observed.strip())
    except ValueError:
        return EGRESS_UNKNOWN
    return None if address == ipaddress.ip_address(expected) else EGRESS_MISMATCH


# --- Health status ------------------------------------------------------------------------------


class HealthHistory(Protocol):
    def recent_health(self, environment_id: uuid.UUID, limit: int) -> tuple[bool, ...]:
        """The latest health results, newest first."""
        ...


class EnvironmentHealth:
    def __init__(
        self, registry: ShopEnvironmentRegistry, history: HealthHistory, actor: ActorRef
    ) -> None:
        self._registry = registry
        self._history = history
        self._actor = actor

    def record(
        self, environment: BrowserEnvironment, report: HealthReport, *, correlation_id: str
    ) -> EnvironmentStatus:
        """Store the report; move the status when the last two reports call for it."""
        self._registry.record_health(
            environment.id, report, by=self._actor, correlation_id=correlation_id
        )
        recent = self._history.recent_health(environment.id, HEALTH_WINDOW)
        target = next_health_status(environment.status, recent)
        if target is None:
            return environment.status
        moved = self._registry.transition(
            environment.id, target, reason=_REASONS[target], by=self._actor,
            expected=environment.status, correlation_id=correlation_id,
        )  # fmt: skip
        return moved.status


# --- Session validation -------------------------------------------------------------------------


@dataclass(frozen=True)
class SignInTask:
    id: uuid.UUID
    environment_id: uuid.UUID
    shop_id: uuid.UUID
    organization_id: uuid.UUID


class SignInTasks(Protocol):
    def open_sign_in(self, environment: BrowserEnvironment, *, correlation_id: str) -> uuid.UUID:
        """One open task per environment: opening it again returns the same task."""
        ...


class SessionValidation:
    def __init__(
        self, registry: ShopEnvironmentRegistry, tasks: SignInTasks, actor: ActorRef
    ) -> None:
        self._registry = registry
        self._tasks = tasks
        self._actor = actor

    def signed_out(self, environment: BrowserEnvironment, *, correlation_id: str) -> None:
        if environment.status in _SIGN_IN_FROM:
            self._registry.transition(
                environment.id, EnvironmentStatus.LOGIN_REQUIRED, reason=SIGNED_OUT,
                by=self._actor, expected=environment.status, correlation_id=correlation_id,
            )  # fmt: skip
        elif environment.status not in _TASK_ONLY:
            return
        self._tasks.open_sign_in(environment, correlation_id=correlation_id)


@dataclass
class MemorySignInTasks:
    tasks: dict[uuid.UUID, SignInTask] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def open_sign_in(self, environment: BrowserEnvironment, *, correlation_id: str) -> uuid.UUID:  # noqa: ARG002
        with self._lock:
            task = self.tasks.get(environment.id)
            if task is None:
                task = SignInTask(uuid.uuid7(), environment.id, environment.shop_id,
                                  environment.organization_id)  # fmt: skip
                self.tasks[environment.id] = task
            return task.id
