"""Action definitions and channel bindings (ADR-0019, `execution-engine.md` §2), and the action
catalog built from the registry.

An action is a registry tool (`registry/tools/*.toml`: side effect, risk, data scope, readiness)
plus its execution contract in `registry/actions/*.toml` (reviewed data): the approval policy,
how the result is verified, its channel bindings in order of preference, its limits keys, and
the JSON Schemas of its input and output in `packages/contracts/actions/`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from htco_application.access.policy import Capability
from htco_application.execution.gateway import Channel
from htco_application.execution.guards import RateBudget
from htco_application.execution.schema import check_schema
from htco_application.jobs import JsonValue
from htco_application.secrets import secret_carrying_fields, secret_tool_names
from htco_domain.actions import ApprovalPolicy, VerifyMode
from htco_domain.tooling import ToolDefinition


@dataclass(frozen=True)
class Evidence:
    kind: str  # "api_response", "reread", "dom_snapshot_hash", "export_file", "human_note"
    ref: str  # a storage key or row ID; never secrets or raw buyer content
    sha256: str | None = None


@dataclass(frozen=True)
class ChannelBinding:
    channel: Channel
    recipe: str | None = None  # api: the adapter method; browser: the recipe ID
    requires_capability: str | None = None  # a shop capability, for example "receipts_read"
    browser_scope_id: str | None = None
    deterministic: bool = True  # False only for Browser Use recipes (employees only)
    breaker_key: str | None = None  # the circuit breaker it shares: "etsy:api", "etsy:web"
    # Declared but not built yet: never routed, and exempt from the startup recipe check (#147).
    disabled: bool = False


@dataclass(frozen=True)
class ChannelOutcome:
    """What a channel adapter returns after running an action."""

    output: Mapping[str, JsonValue] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    handed_to_human: bool = False
    handoff_reason: str | None = None  # "security_challenge", "human_channel", ...


@dataclass(frozen=True)
class ActionDefinition:
    tool: ToolDefinition
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    bindings: tuple[ChannelBinding, ...]
    approval_policy: ApprovalPolicy
    verify: VerifyMode
    timeout: timedelta
    rate_limit_key: str | None = None
    concurrency_key: str | None = None
    # The policy capability a user needs to submit it (deny by default).
    user_capability: Capability | None = None

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def version(self) -> int:
        return self.tool.version


class ActionCatalogError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("invalid action catalog:\n" + "\n".join(f"- {p}" for p in problems))


def _binding(raw: Mapping[str, Any]) -> ChannelBinding:
    return ChannelBinding(
        channel=Channel(str(raw["channel"])),
        recipe=raw.get("recipe"),
        requires_capability=raw.get("requires_capability"),
        browser_scope_id=raw.get("browser_scope_id"),
        deterministic=bool(raw.get("deterministic", True)),
        breaker_key=raw.get("breaker_key"),
        disabled=bool(raw.get("disabled", False)),
    )


@dataclass(frozen=True)
class ActionCatalog:
    actions: Mapping[str, ActionDefinition]
    # The rate budgets by key (`[[rate_limits]]`, slice 5.15): every action's key has one.
    rate_budgets: Mapping[str, RateBudget] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, name: str, version: int) -> ActionDefinition | None:
        definition = self.actions.get(name)
        if definition is None or definition.version != version:
            return None
        return definition

    @classmethod
    def from_documents(
        cls,
        documents: Iterable[Mapping[str, Any]],
        *,
        tools: Mapping[str, ToolDefinition],
        schemas: Mapping[str, Mapping[str, Any]],
        rate_limits: Iterable[Mapping[str, Any]] = (),
    ) -> ActionCatalog:
        """`documents` are the `[[actions]]` tables, `rate_limits` the `[[rate_limits]]` tables;
        `schemas` the JSON Schemas by file name."""
        problems: list[str] = []
        budgets: dict[str, RateBudget] = {}
        for raw in rate_limits:
            try:
                budget = RateBudget(str(raw["key"]), int(raw["per_second"]), int(raw["per_day"]))
            except (KeyError, TypeError, ValueError) as error:
                problems.append(f"rate limit {raw.get('key', '?')!r}: {error}")
                continue
            if budget.key in budgets:
                problems.append(f"duplicate rate limit {budget.key!r}")
            budgets[budget.key] = budget
        actions: dict[str, ActionDefinition] = {}
        for document in documents:
            name = str(document.get("tool", "?"))
            try:
                definition = _parse(document, tools, schemas)
            except (KeyError, TypeError, ValueError) as error:
                problems.append(f"action {name!r}: {error}")
                continue
            if definition.name in actions:
                problems.append(f"duplicate action {definition.name!r}")
                continue
            key = definition.rate_limit_key
            if key is not None and key not in budgets:
                problems.append(f"action {name!r}: no rate budget {key!r} in [[rate_limits]]")
                continue
            actions[definition.name] = definition
        if problems:
            raise ActionCatalogError(problems)
        return cls(MappingProxyType(actions), MappingProxyType(budgets))


def _parse(
    document: Mapping[str, Any],
    tools: Mapping[str, ToolDefinition],
    schemas: Mapping[str, Mapping[str, Any]],
) -> ActionDefinition:
    tool = tools.get(str(document["tool"]))
    if tool is None:
        raise ValueError("no such tool in the registry")
    policy = ApprovalPolicy(str(document["approval_policy"]))
    if f"Approval policy {policy.value}." not in tool.description:
        raise ValueError(f"approval policy {policy.value} differs from the tool's description")
    if policy.is_write != tool.mutating:
        raise ValueError("a write policy needs a mutating tool, and a read policy a reading one")
    bindings = tuple(_binding(raw) for raw in document["bindings"])
    if not bindings:
        raise ValueError("an action needs at least one channel binding")
    for binding in bindings:
        if not binding.deterministic and binding.channel is not Channel.BROWSER:
            raise ValueError("only a browser binding may be non-deterministic (Browser Use)")
        if binding.channel is Channel.BROWSER and binding.browser_scope_id is None:
            raise ValueError("a browser binding needs its browser_scope_id")
    input_schema = schemas[str(document["input_schema"])]
    output_schema = schemas[str(document["output_schema"])]
    for schema in (input_schema, output_schema):
        check_schema(schema)
    leaks = secret_carrying_fields(output_schema)
    if leaks or secret_tool_names([tool.name]):
        # ADR-0021: no tool hands a secret or a one-time code to its caller (slice 5.17).
        raise ValueError(
            f"the output could carry a secret or a code: {', '.join(leaks) or tool.name}"
        )
    capability = document.get("user_capability")
    return ActionDefinition(
        tool=tool,
        input_schema=input_schema,
        output_schema=output_schema,
        bindings=bindings,
        approval_policy=policy,
        verify=VerifyMode(str(document["verify"])),
        timeout=timedelta(seconds=int(document["timeout_seconds"])),
        rate_limit_key=document.get("rate_limit_key"),
        concurrency_key=document.get("concurrency_key"),
        user_capability=None if capability is None else Capability(str(capability)),
    )
