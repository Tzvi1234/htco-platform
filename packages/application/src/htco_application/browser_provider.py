"""The `BrowserProvider` contract (slice 8A.2a, ADR-0025 §1, `execution-engine.md` §3).

A provider runs browsers on the shops' sealed profiles. Its contract has seven async layers:
Provisioning, Lifecycle, Connection, Execution, Authentication, Networking and Monitoring. A
`ProviderCapabilities` declaration says which of them a provider implements. The declaration is
a claim that the shared contract suite (`tests/contracts/browser_provider/`) checks. It grants
nothing.

Rules:
- `BrowserController` is the only entry point. It holds the profile lock, the deadline, the
  parallel-profile slot and the session record, and calls the provider inside them. Business
  logic depends on the controller, never on a provider or on Chromium. The only implementation,
  `ManagedChromiumProvider`, lives in the browser-worker package, and no inner layer imports it
  (import-linter and `tests/architecture/test_browser_provider_imports.py`).
- **The fence.** `save_session`, `renew`, `recover` and `destroy_profile` take the session's own
  `ProfileFence`: the lease of its own profile lock (`ProfileLease`), never a lookup by
  environment. The archive is written with the lease checked inside the same write (#169), so a
  session that lost its lock never writes the profile back (`ProfileFenceLostError`).
- **CDP over a pipe only.** `cdp_transport` is `pipe`; there is no TCP port, no loopback
  endpoint and no WebSocket URL for a session's browser.
- **Async ports (AUD-05).** Every layer is async; an adapter owns its thread hop for blocking
  work, so no profile or database call blocks the event loop.
- A provider receives only what the port passes: no OAuth token, Secret Broker access, database
  role or network outside its egress.

Built in this slice: Provisioning (create, destroy), Lifecycle (open, save_session, renew,
recover, close), Connection, Execution (Playwright steps) and Monitoring. Authentication (the
8A.4 detectors) and Networking (the 8A.3 egress check) arrive with their slices; until then no
provider may declare them, because the suite has nothing to confirm them with.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from htco_domain.browser import BrowserEnvironment

MANAGED_CHROMIUM = "managed-chromium"
_PROVIDER_ID_MAX = 64


class ProviderLayer(StrEnum):
    PROVISIONING = "provisioning"
    LIFECYCLE = "lifecycle"
    CONNECTION = "connection"
    EXECUTION = "execution"
    AUTHENTICATION = "authentication"
    NETWORKING = "networking"
    MONITORING = "monitoring"


type ExecutionEngine = Literal["playwright", "browser_use", "jev"]
_EXECUTION_ENGINES: frozenset[str] = frozenset({"playwright", "browser_use", "jev"})


class CapabilityDeclarationError(ValueError):
    """A capabilities declaration that no provider may make (not a pipe, no slot, ...)."""


@dataclass(frozen=True)
class ProviderNeeds:
    """What one recipe or job needs from the provider. The controller refuses it before any
    session starts when the provider does not declare all of it (AC-8A.2.4)."""

    layers: frozenset[ProviderLayer] = frozenset()
    execution_engines: frozenset[ExecutionEngine] = frozenset({"playwright"})
    renew: bool = False
    recover: bool = False
    downloads: bool = False


@dataclass(frozen=True)
class ProviderCapabilities:
    """A provider's claim of what it supports (ADR-0025 §1). It grants nothing."""

    provider_id: str
    engine: Literal["chromium"]
    engine_version: str
    layers: frozenset[ProviderLayer]
    execution_engines: frozenset[ExecutionEngine]
    cdp_transport: Literal["pipe"]
    renew: bool
    recover: bool
    downloads: bool
    parallel_profiles: int

    def __post_init__(self) -> None:
        if not self.provider_id or len(self.provider_id) > _PROVIDER_ID_MAX:
            raise CapabilityDeclarationError("a provider has a short ID")
        if self.engine != "chromium":
            raise CapabilityDeclarationError("the only engine is the fleet's pinned Chromium")
        if not self.engine_version:
            raise CapabilityDeclarationError("a provider names its pinned engine version")
        if self.cdp_transport != "pipe":
            raise CapabilityDeclarationError("CDP runs on a pipe only, never a port")
        if not set(self.execution_engines) <= _EXECUTION_ENGINES:
            raise CapabilityDeclarationError("an unknown execution engine")
        needed = {ProviderLayer.LIFECYCLE, ProviderLayer.CONNECTION}
        if not needed <= self.layers:
            raise CapabilityDeclarationError("every provider runs sessions over a connection")
        if self.execution_engines and ProviderLayer.EXECUTION not in self.layers:
            raise CapabilityDeclarationError("an execution engine needs the execution layer")
        if (self.renew or self.recover) and ProviderLayer.MONITORING not in self.layers:
            raise CapabilityDeclarationError("renew and recover end with a health check")
        if isinstance(self.parallel_profiles, bool) or self.parallel_profiles < 1:
            raise CapabilityDeclarationError("a provider runs at least one profile at a time")

    @property
    def complete(self) -> bool:
        """All seven layers: the bar for readiness `enabled` (end of Phase 8A)."""
        return self.layers == frozenset(ProviderLayer)

    def missing(self, needs: ProviderNeeds) -> tuple[str, ...]:
        """What `needs` asks for that this declaration lacks, as stable codes."""
        gaps = [f"layer:{layer.value}" for layer in sorted(needs.layers - self.layers)]
        gaps += [f"engine:{name}" for name in sorted(needs.execution_engines)
                 if name not in self.execution_engines]  # fmt: skip
        gaps += [name for name, wanted, has in (
            ("renew", needs.renew, self.renew),
            ("recover", needs.recover, self.recover),
            ("downloads", needs.downloads, self.downloads),
        ) if wanted and not has]  # fmt: skip
        return tuple(gaps)


# --- Errors -------------------------------------------------------------------------------------


class ProviderError(Exception):
    """A provider refused a call; the message is a fixed phrase, never page or profile text."""


class ProviderAtCapacityError(ProviderError):
    """The provider already runs its declared number of profiles. The controller's slots keep
    a session from getting here; this is the provider's own guard."""


class DeadlinePassedError(ProviderError):
    """The deadline passed before the browser could start; nothing was launched."""


class HandleClosedError(ProviderError):
    """The handle's browser has been saved or closed; open a new session."""


@dataclass(frozen=True)
class HealthReport:
    """A health check's result. `code` names the failure (`engine_unreachable`,
    `profile_unusable`, ...); it is never page text."""

    ok: bool
    code: str | None = None
    engine_version: str | None = None


# --- The fence ----------------------------------------------------------------------------------


class ProfileLease(Protocol):
    """One `hold()`'s claim on an environment's profile lock: `held` says whether it got the
    lock. It is bound to its own lock (in Postgres, its own connection), never to whichever
    session holds the environment now."""

    @property
    def environment_id(self) -> uuid.UUID: ...

    @property
    def held(self) -> bool: ...


# The session's own lease, passed to every write-back (ADR-0025 §1).
type ProfileFence = ProfileLease


# --- The seven layers ---------------------------------------------------------------------------


class ProviderHandle(Protocol):
    """One open session's browser, opaque to the controller."""

    @property
    def environment_id(self) -> uuid.UUID: ...


class ProfileProvisioning(Protocol):
    async def create_profile(self, environment: BrowserEnvironment) -> None:
        """Seal a new, empty profile for a `PROVISIONING` environment (8A.1)."""
        ...

    async def destroy_profile(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> None:
        """Delete the sealed profile while `fence` holds the profile lock."""
        ...


class SessionLifecycle(Protocol):
    async def open(
        self, environment: BrowserEnvironment, *, deadline: datetime, fence: ProfileFence
    ) -> ProviderHandle:
        """Unpack the last good archive and start one browser on it. Raises
        `DeadlinePassedError` (and launches nothing) when the unpack used up the time."""
        ...

    async def save_session(self, handle: ProviderHandle, *, fence: ProfileFence) -> None:
        """Stop the browser and re-seal the profile under the session's own lease, checked
        inside the write: a lost lock raises `ProfileFenceLostError` and writes nothing. This
        is the session's last browser step; only `close` follows."""
        ...

    async def renew(self, handle: ProviderHandle, *, fence: ProfileFence) -> HealthReport:
        """A short scheduled session: check the browser, then re-seal the refreshed cookies
        under the fence, as `save_session` does. A failed check writes nothing back."""
        ...

    async def recover(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> HealthReport:
        """Open the last good archive in a fresh workspace (a crashed session never wrote its
        profile back), run a health check and close it. Nothing is written back."""
        ...

    async def close(self, handle: ProviderHandle) -> None:
        """Always: stop the browser if it still runs and wipe the workspace, without sealing."""
        ...


class EngineConnection(Protocol):
    async def connect(self, handle: ProviderHandle) -> object:
        """The session's CDP pipe connection, in the browser worker's process only: no port and
        no URL. Playwright drives this object in process; no other session can reach it."""
        ...


class EngineExecution(Protocol):
    async def run_steps[T](
        self, handle: ProviderHandle, steps: Callable[[object], Awaitable[T]]
    ) -> T:
        """Deterministic steps (Playwright, the only engine automations use) on the session's
        connection. The recipes themselves are 8A.5; Browser Use is 8A.6."""
        ...


class ProviderMonitoring(Protocol):
    async def health_check(self, handle: ProviderHandle) -> HealthReport: ...

    async def engine_version(self) -> str:
        """The engine build this provider really runs."""
        ...


class BrowserProvider(
    ProfileProvisioning,
    SessionLifecycle,
    EngineConnection,
    EngineExecution,
    ProviderMonitoring,
    Protocol,
):
    """The contract as built so far; Authentication and Networking join with 8A.4 and 8A.3."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...
