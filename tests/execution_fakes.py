"""In-memory parts of the Action Gateway (slice 5.12): the store with the Postgres store's rules,
approvals, a channel adapter, principals and capabilities. Shared by the gateway's tests."""

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from htco_application.access.policy import Principal
from htco_application.execution.contracts import (
    ActionCatalog,
    ActionDefinition,
    ChannelBinding,
    ChannelOutcome,
)
from htco_application.execution.gateway import ActionRequest, ActionResult, Channel
from htco_application.execution.service import Claim, Finish
from htco_application.workforce.files import read_action_catalog, read_registry
from htco_application.workforce.registry import Registry
from htco_domain.actions import ACTION_FINAL_STATES, ActionState
from tests.event_contracts import check_if_event

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry"
SCHEMAS = ROOT / "packages" / "contracts" / "actions"


def real_catalog(registry: Registry | None = None, *, built: bool = False) -> ActionCatalog:
    """The registry's catalog. `built` enables every disabled binding, as if its recipe were
    built: for tests of the gateway's flow (approvals, guards) through a fake channel, which
    use a declared write such as the inventory update as their example (#147)."""
    catalog = read_action_catalog(REGISTRY, SCHEMAS, registry or read_registry(REGISTRY).registry())
    if not built:
        return catalog
    actions = {
        name: replace(action, bindings=tuple(replace(b, disabled=False) for b in action.bindings))
        for name, action in catalog.actions.items()
    }
    return replace(catalog, actions=actions)


@dataclass
class Row:
    request: ActionRequest
    request_hash: str
    state: ActionState
    result: ActionResult | None = None
    approval_id: uuid.UUID | None = None
    steps: list[Finish] = field(default_factory=list)


@dataclass
class MemoryActionStore:
    rows: dict[tuple[str, str], Row] = field(default_factory=dict)
    ids: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def claim(self, request: ActionRequest, *, request_hash: str, now: datetime) -> Claim:  # noqa: ARG002
        key = (request.action.name, request.idempotency_key)
        with self._guard:
            row = self.rows.get(key)
            if row is None:
                execution_id = uuid.uuid7()
                self.rows[key] = Row(request, request_hash, ActionState.RECEIVED)
                self.ids[execution_id] = key
                return Claim(execution_id, created=True, state=ActionState.RECEIVED, result=None)
            execution_id = next(i for i, k in self.ids.items() if k == key)
            final = row.result if row.state in ACTION_FINAL_STATES else None
            return Claim(
                execution_id,
                created=False,
                state=row.state,
                result=final,
                approval_id=row.approval_id,
                retry_at=None if row.result is None else row.result.retry_at,
                error_code=None if row.result is None else row.result.error_code,
                request_hash=row.request_hash,
                channel=None if row.result is None else row.result.channel,
            )

    def record(self, execution_id: uuid.UUID, step: Finish, *, now: datetime) -> None:  # noqa: ARG002
        if step.event is not None:
            check_if_event(step.event)  # the gateway's real events, against their schemas
        with self._guard:
            row = self.rows[self.ids[execution_id]]
            row.state, row.result = step.state, step.result
            row.approval_id = step.approval_id or row.approval_id
            row.steps.append(step)

    def only(self) -> Row:
        [row] = self.rows.values()
        return row


@dataclass
class Approvals:
    requested: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    approved: set[tuple[uuid.UUID, str]] = field(default_factory=set)
    executions: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    def request(
        self,
        request: ActionRequest,  # noqa: ARG002 - the port's signature
        *,
        request_hash: str,
        definition: ActionDefinition,  # noqa: ARG002
    ) -> uuid.UUID:
        approval_id = uuid.uuid7()
        self.requested.append((approval_id, request_hash))
        return approval_id

    def approve_last(self) -> uuid.UUID:
        approval_id, digest = self.requested[-1]
        self.approved.add((approval_id, digest))
        return approval_id

    def is_approved(self, approval_id: uuid.UUID, *, request_hash: str) -> bool:
        return (approval_id, request_hash) in self.approved

    def executed(self, approval_id: uuid.UUID, *, execution_id: uuid.UUID) -> None:
        self.executions.append((approval_id, execution_id))


@dataclass
class FakeChannel:
    """A channel that records its calls and answers from a script."""

    channel: Channel = Channel.API
    output: Mapping[str, object] = field(default_factory=dict)
    verified: bool = True
    fail: Exception | None = None
    hand_off: bool = False
    calls: list[tuple[ActionRequest, ChannelBinding]] = field(default_factory=list)

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:
        self.calls.append((request, binding))
        if self.fail is not None:
            raise self.fail
        return ChannelOutcome(output=dict(self.output), handed_to_human=self.hand_off)  # type: ignore[arg-type]

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return self.verified


@dataclass
class Principals:
    by_user: dict[tuple[uuid.UUID, uuid.UUID], Principal] = field(default_factory=dict)

    def principal(self, user_id: uuid.UUID, organization_id: uuid.UUID) -> Principal | None:
        return self.by_user.get((user_id, organization_id))


@dataclass
class Shops:
    owners: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return self.owners.get(shop_id)


@dataclass
class Capabilities:
    granted: set[tuple[uuid.UUID, str]] = field(default_factory=set)

    def available(self, shop_id: uuid.UUID, capability: str) -> bool:
        return (shop_id, capability) in self.granted
