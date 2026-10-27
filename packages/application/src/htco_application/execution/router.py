"""The Execution Router (slice 5.13, `execution-engine.md` §3 and §4 step 5).

`ExecutionRouter.choose` picks one channel binding deterministically: the action's bindings in
order (those of the request's `channel_hint` first, a preference only), keeping the first one
that can run now:

- its channel adapter is deployed;
- an automation only takes deterministic bindings, never Browser Use (AC-5.13.2);
- an API binding needs its shop capability (`shop_capabilities`);
- a browser binding needs the shop's environment `READY`, the action's flag on, and the Etsy
  scope row for its `browser_scope_id` recorded after the flag was turned on (AC-5.13.3).

So a missing capability on the API binding never falls through to a browser binding unless that
binding has its own recorded scope (AC-5.13.5). Without a binding the answer is a refusal with
the first reason found: `scope_missing`, `environment_not_ready` or `flag_off`; `no_channel` when
no adapter is deployed at all. Execution never falls through: a channel that fails at run time
is a failed attempt, not a reason to try the next channel.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from htco_application.execution.contracts import ActionDefinition, ChannelBinding
from htco_application.execution.gateway import ActionRequest, Channel
from htco_domain.actors import ActorKind
from htco_domain.browser import EnvironmentStatus

if TYPE_CHECKING:
    from htco_application.execution.service import ChannelAdapter, ShopCapabilities


class BrowserGate(Protocol):
    """What the router needs to know about a shop's browser (the registry of slice 5.16)."""

    def environment_status(self, shop_id: uuid.UUID) -> EnvironmentStatus | None: ...

    def flag_enabled_at(self, action_name: str) -> datetime | None:
        """When the action's browser flag was turned on; None while it is off."""
        ...

    def scope_recorded_at(self, shop_id: uuid.UUID, scope_id: str) -> datetime | None: ...


class NoBrowser:
    """No browser environments exist yet (until Phase 8A): every browser binding is skipped."""

    def environment_status(self, shop_id: uuid.UUID) -> EnvironmentStatus | None:  # noqa: ARG002
        return None

    def flag_enabled_at(self, action_name: str) -> datetime | None:  # noqa: ARG002
        return None

    def scope_recorded_at(self, shop_id: uuid.UUID, scope_id: str) -> datetime | None:  # noqa: ARG002
        return None


class ShopScopes(Protocol):
    """The Etsy OAuth scopes the shop's current grant holds (recorded at connect, slice 4.3)."""

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]: ...


class WriteSwitch(Protocol):
    """Whether an action's write flag is on now (W2's per-action flag, the owner's kill switch)."""

    def is_on(self, action_name: str) -> bool: ...


@dataclass(frozen=True)
class StaticWriteSwitch:
    """The flags named at start (`HTCO_ETSY_WRITE_ACTIONS`): every write off by default."""

    on: frozenset[str] = frozenset()

    def is_on(self, action_name: str) -> bool:
        return action_name in self.on


class NoScopes:
    """No grant is known: every binding that needs a scope is refused."""

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]:  # noqa: ARG002
        return frozenset()


@dataclass(frozen=True)
class Route:
    binding: ChannelBinding
    adapter: ChannelAdapter


@dataclass(frozen=True)
class NoRoute:
    code: str
    reason: str


class ExecutionRouter:
    def __init__(
        self,
        adapters: Mapping[Channel, ChannelAdapter],
        capabilities: ShopCapabilities,
        browser: BrowserGate,
        *,
        scopes: ShopScopes | None = None,
        write_flags: frozenset[str] | WriteSwitch = frozenset(),
    ) -> None:
        self._adapters = adapters
        self._capabilities = capabilities
        self._browser = browser
        self._scopes = scopes or NoScopes()
        # The API writes switched on, by action name (W2's per-action flag): off unless named.
        self._write_flags = (
            StaticWriteSwitch(write_flags) if isinstance(write_flags, frozenset) else write_flags
        )

    def write_enabled(self, action_name: str) -> bool:
        return self._write_flags.is_on(action_name)

    def choose(self, definition: ActionDefinition, request: ActionRequest) -> Route | NoRoute:
        bindings = sorted(
            definition.bindings, key=lambda b: request.channel_hint is not None and
            b.channel is not request.channel_hint,
        )  # fmt: skip
        refusals: list[NoRoute] = []
        for binding in bindings:
            adapter = self._adapters.get(binding.channel)
            if adapter is None or binding.disabled:
                continue
            refusal = self._refusal(definition, request, binding)
            if refusal is None:
                return Route(binding, adapter)
            refusals.append(refusal)
        if refusals:
            return refusals[0]
        return NoRoute("no_channel", f"no deployed channel can run {definition.name}")

    def _refusal(
        self, definition: ActionDefinition, request: ActionRequest, binding: ChannelBinding
    ) -> NoRoute | None:
        if request.actor.kind is ActorKind.AUTOMATION and not binding.deterministic:
            return NoRoute("scope_missing", "automations take deterministic bindings only")
        if binding.requires_capability is not None and (
            request.shop_id is None
            or not self._capabilities.available(request.shop_id, binding.requires_capability)
        ):
            return NoRoute("scope_missing", f"the shop lacks {binding.requires_capability}")
        if binding.requires_scope is not None and (
            request.shop_id is None
            or binding.requires_scope not in self._scopes.scopes_of(request.shop_id)
        ):
            return NoRoute(
                "scope_missing",
                f"the shop's grant lacks {binding.requires_scope}: reconnect the shop to grant it",
            )
        if (
            definition.approval_policy.is_write
            and binding.channel is not Channel.BROWSER
            and not self._write_flags.is_on(definition.name)
        ):
            return NoRoute("flag_off", f"the write flag of {definition.name} is off")
        if binding.channel is Channel.BROWSER:
            return self._browser_refusal(definition, request, binding)
        return None

    def _browser_refusal(
        self, definition: ActionDefinition, request: ActionRequest, binding: ChannelBinding
    ) -> NoRoute | None:
        if request.shop_id is None or binding.browser_scope_id is None:
            return NoRoute("scope_missing", "a browser action needs a shop and its scope")
        if self._browser.environment_status(request.shop_id) is not EnvironmentStatus.READY:
            return NoRoute("environment_not_ready", "the shop's browser is not ready")
        flag_at = self._browser.flag_enabled_at(definition.name)
        if flag_at is None:
            return NoRoute("flag_off", f"the browser flag of {definition.name} is off")
        scope_at = self._browser.scope_recorded_at(request.shop_id, binding.browser_scope_id)
        if scope_at is None or scope_at < flag_at:
            return NoRoute("scope_missing", f"no recorded scope {binding.browser_scope_id}")
        return None
