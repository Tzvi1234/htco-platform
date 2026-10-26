"""Status management (slice 5.18): status events and the chain view for the office.

**Events.** Every status change publishes exactly one event through the outbox, in the
transaction that records it (AC-5.18.2):

- an automation execution's state change is `automations.execution_state_changed.v1`
  (`execution_state_event`, written by the execution store with its state-change row);
- an action execution's recorded step is `actions.executed.v1`, `actions.approval_required.v1`,
  `actions.handed_to_human.v1` or `actions.deferred.v1` (the gateway);
- a browser environment's change is `browser.environment_status_changed.v1` (the registry).

**The chain view.** `GET /v1/executions/{id}` shows one automation execution with everything
around it, in causal order (AC-5.18.1): its trigger (the inbox event, such as an Etsy webhook,
when the execution names it as its cause), its state changes, every action it ran with the
channel and outcome, their approvals and hand-offs to a person, and the events they published.
`ChainReader` reads the records of one organization; `execution_chain` checks the caller and
puts them in order.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.automation.durable import StateChange
from htco_application.kernel import EVENTS_DESTINATION, JsonValue
from htco_application.messaging import OutboxMessage
from htco_application.shops import OrganizationRequiredError
from htco_domain.actions import ActionState
from htco_domain.automation import ExecutionState
from htco_domain.status import StatusKind, WaitingReason, status_kind, waiting_reason

EXECUTION_STATE_CHANGED = "automations.execution_state_changed.v1"


def execution_state_event(change: StateChange) -> OutboxMessage:
    """IDs and states only (EVENT_CATALOG rule 3): never the payload or the error text."""
    payload: dict[str, JsonValue] = {
        "event_type": EXECUTION_STATE_CHANGED,
        "execution_id": str(change.execution_id),
        "from_state": None if change.from_state is None else change.from_state.value,
        "to_state": change.to_state.value,
        "status": status_kind(change.to_state).value,
        "actor": str(change.actor),
        "occurred_at": change.at.isoformat(),
    }
    return OutboxMessage(
        organization_id=change.organization_id,
        destination=EVENTS_DESTINATION,
        # One message per recorded change: the same state can be reached twice at one instant
        # (a retry), so the key names the change itself rather than the state and the time.
        idempotency_key=f"{EXECUTION_STATE_CHANGED}:{change.execution_id}:{uuid.uuid7()}",
        payload=payload,
        correlation_id=change.correlation_id,
    )


# --- The chain view -----------------------------------------------------------------------------


class ChainEntryKind(StrEnum):
    TRIGGER = "trigger"
    EXECUTION = "execution"  # one state change of the automation execution
    ACTION = "action"
    APPROVAL = "approval"
    HUMAN_TASK = "human_task"
    EVENT = "event"


@dataclass(frozen=True)
class TriggerRecord:
    id: str
    kind: str  # "event" (an inbox row: source and event type), "schedule", "user_action", ...
    at: datetime
    name: str  # "etsy:receipt.paid", or the trigger kind


@dataclass(frozen=True)
class ExecutionRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    automation_id: str
    state: ExecutionState
    correlation_id: str
    created_at: datetime
    # A succeeded run's checked handler output (#127): for the Owner and Admins only.
    output: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ChangeRecord:
    from_state: ExecutionState | None
    to_state: ExecutionState
    actor: str
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class ActionRecord:
    id: uuid.UUID
    action: str
    state: ActionState
    outcome: str | None
    channel: str | None
    error_code: str | None
    approval_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventRecord:
    id: uuid.UUID
    event_type: str
    action_execution_id: uuid.UUID | None
    at: datetime


@dataclass(frozen=True)
class ChainRecords:
    execution: ExecutionRecord
    trigger: TriggerRecord
    changes: Sequence[ChangeRecord] = ()
    actions: Sequence[ActionRecord] = ()
    events: Sequence[EventRecord] = ()


@dataclass(frozen=True)
class ChainEntry:
    kind: ChainEntryKind
    id: str
    at: datetime
    name: str
    state: str | None = None
    status: StatusKind | None = None
    detail: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionChain:
    execution: ExecutionRecord
    status: StatusKind
    entries: tuple[ChainEntry, ...]


class ChainReader(Protocol):
    def records(
        self, organization_id: uuid.UUID, execution_id: uuid.UUID
    ) -> ChainRecords | None: ...


class ExecutionNotVisibleError(Exception):
    """No such execution for the caller (another organization, or a shop not granted)."""


_RANK = {kind: index for index, kind in enumerate(ChainEntryKind)}


def build_chain(records: ChainRecords) -> ExecutionChain:
    """Every record as an entry, in causal order: by time, and at the same time by kind (a
    trigger before its execution, an action before its approval, hand-off and events)."""
    execution = records.execution
    entries: list[ChainEntry] = [
        ChainEntry(ChainEntryKind.TRIGGER, records.trigger.id, records.trigger.at,
                   records.trigger.name, detail={"kind": records.trigger.kind}),
    ]  # fmt: skip
    entries.extend(
        ChainEntry(
            ChainEntryKind.EXECUTION,
            str(execution.id),
            change.at,
            execution.automation_id,
            state=change.to_state.value,
            status=status_kind(change.to_state),
            detail={
                "from_state": None if change.from_state is None else change.from_state.value,
                "actor": change.actor,
                "reason": change.reason,
            },
        )
        for change in records.changes
    )
    for action in records.actions:
        entries.append(
            ChainEntry(
                ChainEntryKind.ACTION,
                str(action.id),
                action.created_at,
                action.action,
                state=action.state.value,
                status=status_kind(action.state),
                detail={
                    "channel": action.channel,
                    "outcome": action.outcome,
                    "error_code": action.error_code,
                },
            )
        )
        if action.approval_id is not None:
            entries.append(
                ChainEntry(ChainEntryKind.APPROVAL, str(action.approval_id), action.updated_at,
                           action.action, detail={"action_execution_id": str(action.id)})
            )  # fmt: skip
        if waiting_reason(action.state) is WaitingReason.WAITING_FOR_HUMAN:
            entries.append(
                ChainEntry(ChainEntryKind.HUMAN_TASK, str(action.id), action.updated_at,
                           action.action, state=WaitingReason.WAITING_FOR_HUMAN.value,
                           status=StatusKind.WAITING)
            )  # fmt: skip
    entries.extend(
        ChainEntry(
            ChainEntryKind.EVENT,
            str(event.id),
            event.at,
            event.event_type,
            detail={
                "action_execution_id": None
                if event.action_execution_id is None
                else str(event.action_execution_id)
            },
        )
        for event in records.events
    )
    entries.sort(key=lambda e: (e.at, _RANK[e.kind]))
    return ExecutionChain(execution, status_kind(execution.state), tuple(entries))


def execution_chain(
    auth: AuthContext,
    reader: ChainReader,
    execution_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
) -> ExecutionChain:
    """ "View the office" for the organization, and the execution's shop for a shop-scoped role.
    Raises `OrganizationRequiredError`, `NoMembershipError`, `ExecutionNotVisibleError`, or
    `PermissionError` for a role outside the office."""
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)  # NoMembershipError for a stranger
    office = decide(principal, Capability.VIEW_AGENT_DIRECTORY, shop=None, now=now)
    if office.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not view the office")
    records = reader.records(organization, execution_id)
    if records is None:
        raise ExecutionNotVisibleError(str(execution_id))
    shop = records.execution.shop_id
    if shop is None and office.scope is Scope.GRANTED_SHOPS:
        raise ExecutionNotVisibleError(str(execution_id))  # an organization-wide run
    if shop is not None and (
        decide(
            principal, Capability.VIEW_AGENT_DIRECTORY,
            shop=ShopRef(id=shop, organization_id=organization), now=now,
        ).outcome
        is not Outcome.ALLOW
    ):  # fmt: skip
        raise ExecutionNotVisibleError(str(execution_id))
    if office.scope is not Scope.ORGANIZATION and records.execution.output is not None:
        # Per-run counts are the office's view (#127): a shop-scoped role sees the run without.
        records = replace(records, execution=replace(records.execution, output=None))
    return build_chain(records)
