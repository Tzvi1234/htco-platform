"""Preflight, health status and session validation (slice 8A.3, ADR-0020 §4 and §5).

- Preflight runs after the profile lock and before any engine: the egress IP must equal the
  configured one, the disk must have space and the target must answer over DNS and TLS. A
  failed preflight is a failed session with no engine started (AC-8A.3.1).
- Every health report is recorded; two failures in a row move READY to DEGRADED and two passes
  move it back (AC-8A.3.3).
- A signed-out profile moves the environment to LOGIN_REQUIRED and opens one Human task to sign
  in (AC-8A.3.2).
"""

import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest

from htco_application.browser_controller import (
    BrowserController,
    MemoryProfileLocks,
    MemorySessionLog,
    PreflightFailedError,
    PreflightTimeoutError,
    SessionOutcome,
)
from htco_application.browser_environments import EnvironmentRegistry, MemoryEnvironmentStore
from htco_application.browser_health import (
    DISK_LOW,
    EGRESS_MISMATCH,
    EGRESS_UNKNOWN,
    SIGN_IN_TITLE,
    TARGET_UNREACHABLE,
    EnvironmentHealth,
    MemorySignInTasks,
    Preflight,
    PreflightConfig,
    SessionValidation,
)
from htco_application.browser_profiles import (
    MemoryProfileArchiveStore,
    MemoryProfileRecords,
    ProfileVault,
    ProfileWorkspaces,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus, HealthReport
from htco_platform.crypto import EnvelopeSealer, KeyRing

pytestmark = pytest.mark.anyio

_E = EnvironmentStatus
WORKER = ActorRef(ActorKind.SYSTEM, "browser-worker")
EGRESS = "203.0.113.10"  # a documentation address (RFC 5737), never a real one


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Probes:
    ip: str | None = EGRESS
    free: int = 10 * 1024**3
    reachable: set[str] = field(default_factory=lambda: {"www.example.test"})
    asked: list[str] = field(default_factory=list)

    async def current_ip(self) -> str | None:
        return self.ip

    def free_bytes(self) -> int:
        return self.free

    async def reachable_over_tls(self, host: str) -> bool:
        self.asked.append(host)
        return host in self.reachable


def config() -> PreflightConfig:
    return PreflightConfig(
        expected_egress_ip=EGRESS, min_free_bytes=1024**3, targets=("www.example.test",)
    )


def preflight(probes: Probes) -> Preflight:
    return Preflight(config(), probes, probes, probes, clock=now)


def a_registry() -> tuple[EnvironmentRegistry, MemoryEnvironmentStore]:
    store = MemoryEnvironmentStore()
    return EnvironmentRegistry(store, Shops(), now), store


class Shops:
    def __init__(self) -> None:
        self.organization = uuid.uuid4()

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:  # noqa: ARG002
        return self.organization


def an_environment(
    store: MemoryEnvironmentStore, status: EnvironmentStatus = _E.READY
) -> BrowserEnvironment:
    env = BrowserEnvironment(id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
                             status=status)  # fmt: skip
    store.environments[env.id] = env
    return env


# --- Preflight ----------------------------------------------------------------------------------


async def test_preflight__passes_with_the_configured_egress_space_and_target() -> None:
    probes = Probes()
    report = await preflight(probes).run()
    assert (report.ok, report.problems) == (True, ())
    assert probes.asked == ["www.example.test"]


@pytest.mark.ac("AC-8A.3.1")
@pytest.mark.parametrize(
    ("probes", "problem"),
    [
        (Probes(ip="198.51.100.7"), EGRESS_MISMATCH),
        (Probes(ip=None), EGRESS_UNKNOWN),
        (Probes(ip="not an address"), EGRESS_UNKNOWN),
        (Probes(free=1024), DISK_LOW),
        (Probes(reachable=set()), TARGET_UNREACHABLE),
    ],
    ids=["wrong-egress", "no-egress", "garbled-egress", "disk", "target"],
)
async def test_preflight__names_each_failure_as_a_code(probes: Probes, problem: str) -> None:
    report = await preflight(probes).run()
    assert not report.ok
    assert report.problems == (problem,)


def test_preflight_config__refuses_a_bad_address_or_target() -> None:
    with pytest.raises(ValueError, match="egress"):
        PreflightConfig(expected_egress_ip="10.0.0.300", min_free_bytes=1, targets=("a.test",))
    with pytest.raises(ValueError, match="target"):
        PreflightConfig(expected_egress_ip=EGRESS, min_free_bytes=1, targets=("https://x/y",))
    with pytest.raises(ValueError, match="space"):
        PreflightConfig(expected_egress_ip=EGRESS, min_free_bytes=-1, targets=())


@pytest.mark.ac("AC-8A.3.1")
async def test_controller__a_wrong_egress_fails_preflight_and_no_engine_starts(
    tmp_path: Path,
) -> None:
    registry, store = a_registry()
    env = an_environment(store)
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), MemoryProfileArchiveStore(), now
    )
    launched: list[Path] = []

    class Engine:
        async def launch(self, profile_directory: Path) -> None:  # pragma: no cover - refused
            launched.append(profile_directory)

    log, locks = MemorySessionLog(), MemoryProfileLocks()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(vault, tmp_path), Engine(),  # type: ignore[arg-type]
        locks, log, clock=now, preflight=preflight(Probes(ip="198.51.100.7")),
        health=EnvironmentHealth(registry, store, WORKER),
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(
            env.id, purpose="test.run", deadline=now() + timedelta(seconds=5)
        ):
            pass  # pragma: no cover - never entered

    with pytest.raises(PreflightFailedError):
        await run()

    assert launched == []
    assert not any(tmp_path.iterdir())  # noqa: ASYNC240 - the test's own temporary directory
    assert locks.held == set()
    (record,) = log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "preflight_failed")
    ((environment_id, report, _, _),) = store.health
    assert (environment_id, report.ok, report.problems) == (env.id, False, (EGRESS_MISMATCH,))


# --- Health status --------------------------------------------------------------------------------


def report(ok: bool) -> HealthReport:
    return HealthReport(ok=ok, checked_at=now(), problems=() if ok else (DISK_LOW,))


@pytest.mark.ac("AC-8A.3.3")
def test_health__two_failures_degrade_and_two_passes_restore() -> None:
    registry, store = a_registry()
    env = an_environment(store)
    health = EnvironmentHealth(registry, store, WORKER)

    statuses = []
    for ok in (False, True, False, False, True, True):
        current = store.environments[env.id]
        statuses.append(health.record(current, report(ok), correlation_id="c-health"))

    assert statuses == [_E.READY, _E.READY, _E.READY, _E.DEGRADED, _E.DEGRADED, _E.READY]
    moves = [(c.from_status, c.to_status, c.reason) for c in store.changes]
    assert moves == [
        (_E.READY, _E.DEGRADED, "health_failed_twice"),
        (_E.DEGRADED, _E.READY, "health_passed_twice"),
    ]
    assert len(store.health) == 6  # every report is in the history


# --- Session validation -------------------------------------------------------------------------


@pytest.mark.ac("AC-8A.3.2")
@pytest.mark.parametrize("status", [_E.READY, _E.PROVISIONING])
def test_signed_out__moves_to_login_required_and_opens_one_task(status: EnvironmentStatus) -> None:
    registry, store = a_registry()
    env = an_environment(store, status)
    tasks = MemorySignInTasks()
    validation = SessionValidation(registry, tasks, WORKER)

    validation.signed_out(env, correlation_id="c-1")
    validation.signed_out(store.environments[env.id], correlation_id="c-2")  # seen again

    assert store.environments[env.id].status is _E.LOGIN_REQUIRED
    assert [(c.to_status, c.reason) for c in store.changes] == [(_E.LOGIN_REQUIRED, "signed_out")]
    (task,) = tasks.tasks.values()
    assert (task.environment_id, task.shop_id) == (env.id, env.shop_id)
    assert SIGN_IN_TITLE.format(shop="LeatherTest") == "Sign in to LeatherTest"


@pytest.mark.parametrize("status", [_E.SUSPENDED, _E.FAILED])
def test_signed_out__leaves_a_suspended_or_failed_environment_alone(
    status: EnvironmentStatus,
) -> None:
    registry, store = a_registry()
    env = an_environment(store, status)
    tasks = MemorySignInTasks()
    SessionValidation(registry, tasks, WORKER).signed_out(env, correlation_id="c-1")
    assert store.changes == []
    assert tasks.tasks == {}


@pytest.mark.ac("AC-8A.3.2")
def test_signed_out__a_degraded_environment_keeps_its_status_and_gets_one_task() -> None:
    registry, store = a_registry()
    env = an_environment(store, _E.DEGRADED)
    tasks = MemorySignInTasks()
    validation = SessionValidation(registry, tasks, WORKER)
    validation.signed_out(env, correlation_id="c-1")
    validation.signed_out(env, correlation_id="c-2")
    assert store.environments[env.id].status is _E.DEGRADED
    assert store.changes == []
    assert len(tasks.tasks) == 1


@pytest.mark.ac("AC-8A.3.2")
def test_signed_out__decides_from_the_stored_status_not_the_callers_copy() -> None:
    """Codex and quill on #170: the caller still holds DEGRADED, but the environment is READY
    now. Signed out must still move it to LOGIN_REQUIRED, with one task."""
    registry, store = a_registry()
    env = an_environment(store, _E.DEGRADED)
    store.environments[env.id] = replace(env, status=_E.READY)
    tasks = MemorySignInTasks()
    SessionValidation(registry, tasks, WORKER).signed_out(env, correlation_id="c-1")
    assert store.environments[env.id].status is _E.LOGIN_REQUIRED
    assert [(c.from_status, c.to_status) for c in store.changes] == [(_E.READY, _E.LOGIN_REQUIRED)]
    assert len(tasks.tasks) == 1


@pytest.mark.ac("AC-8A.3.2")
async def test_controller__a_degraded_profile_restored_by_its_preflight_then_signed_out_needs_a_sign_in(  # noqa: E501
    tmp_path: Path,
) -> None:
    """Codex and quill on #170: a DEGRADED environment's second passing preflight in this
    session restores it to READY. The session carries that status, so a signed-out marker found
    in it moves the environment to LOGIN_REQUIRED with exactly one task."""
    registry, store = a_registry()
    env = an_environment(store, _E.DEGRADED)
    health = EnvironmentHealth(registry, store, WORKER)
    health.record(env, report(True), correlation_id="c-first-pass")  # one pass: still DEGRADED
    assert store.environments[env.id].status is _E.DEGRADED
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), MemoryProfileArchiveStore(), now
    )

    class Handle:
        async def close(self) -> None:
            return None

    class Engine:
        async def launch(self, profile_directory: Path) -> Handle:  # noqa: ARG002
            return Handle()

    controller = BrowserController(
        MemoryProfileRecords({env.id: store.environments[env.id]}),
        ProfileWorkspaces(vault, tmp_path), Engine(), MemoryProfileLocks(), MemorySessionLog(),
        clock=now, preflight=preflight(Probes()), health=health,
    )  # fmt: skip
    tasks = MemorySignInTasks()
    validation = SessionValidation(registry, tasks, WORKER)
    async with controller.session(
        env.id, purpose="test.run", deadline=now() + timedelta(seconds=5)
    ) as s:
        assert s.environment.status is _E.READY  # the preflight's second pass restored it
        validation.signed_out(s.environment, correlation_id="c-signed-out")

    assert store.environments[env.id].status is _E.LOGIN_REQUIRED
    assert len(tasks.tasks) == 1


async def test_controller__a_passing_preflight_starts_the_engine(tmp_path: Path) -> None:
    env = BrowserEnvironment(id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
                             status=_E.READY)  # fmt: skip
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), MemoryProfileArchiveStore(), now
    )

    class Handle:
        async def close(self) -> None:
            return None

    class Engine:
        async def launch(self, profile_directory: Path) -> Handle:  # noqa: ARG002
            return Handle()

    controller = BrowserController(
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(vault, tmp_path), Engine(),
        MemoryProfileLocks(), MemorySessionLog(), clock=now, preflight=preflight(Probes()),
    )  # fmt: skip
    async with controller.session(
        env.id, purpose="test.run", deadline=now() + timedelta(seconds=5)
    ) as s:
        assert isinstance(s.engine, Handle)


async def test_controller__a_preflight_past_the_deadline_is_a_timeout(tmp_path: Path) -> None:
    """The deadline covers the preflight: one that hangs ends the session as a timeout, and
    nothing is unpacked or launched."""
    env = BrowserEnvironment(id=uuid.uuid4(), organization_id=uuid.uuid4(), shop_id=uuid.uuid4(),
                             status=_E.READY)  # fmt: skip
    vault = ProfileVault(
        EnvelopeSealer(KeyRing(("browser-1", os.urandom(32)))), MemoryProfileArchiveStore(), now
    )

    class Hanging:
        async def run(self) -> HealthReport:
            await anyio.sleep(30)
            raise AssertionError  # pragma: no cover - cancelled at the deadline

    class Engine:
        async def launch(self, profile_directory: Path) -> None:  # noqa: ARG002  # pragma: no cover
            raise AssertionError

    log, locks = MemorySessionLog(), MemoryProfileLocks()
    controller = BrowserController(
        MemoryProfileRecords({env.id: env}), ProfileWorkspaces(vault, tmp_path), Engine(),  # type: ignore[arg-type]
        locks, log, clock=now, preflight=Hanging(),
    )  # fmt: skip

    async def run() -> None:
        async with controller.session(
            env.id, purpose="test.slow", deadline=now() + timedelta(seconds=0.3)
        ):
            pass  # pragma: no cover - never entered

    with pytest.raises(PreflightTimeoutError):
        await run()
    (record,) = log.records.values()
    assert (record.outcome, record.failure) == (SessionOutcome.FAILED, "timeout")
    assert locks.held == set()
    assert not any(tmp_path.iterdir())  # noqa: ASYNC240 - the test's own temporary directory
