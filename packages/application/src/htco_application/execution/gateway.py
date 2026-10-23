"""The Action Gateway's port (ADR-0019, `docs/architecture/execution-engine.md` §2 and §3).

Every business action of an automation or an employee goes through `ActionGateway.submit`: the
gateway checks permissions, approvals, idempotency, limits and verification, and routes the
action to a channel (API, Browser, Internal, Human). This module holds the port and its request
and result values, so the durable engine (slice 5.10) can hand handlers a gateway client now. The
gateway itself, its JSON contracts (`packages/contracts/actions/`) and its tables arrive in slice
5.12. Until then `UnavailableGateway` refuses every action, which is correct: no automation is
ready yet.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from htco_application.jobs import JsonValue
from htco_domain.actors import ActorRef

MAX_KEY_LENGTH = 200


class Channel(StrEnum):
    API = "api"
    BROWSER = "browser"
    INTERNAL = "internal"
    HUMAN = "human"


class ActionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    HANDED_TO_HUMAN = "handed_to_human"
    DEFERRED = "deferred"  # breaker open or rate-limited; re-queued

    @property
    def waits(self) -> bool:
        """The action continues later: the automation waits instead of holding a lease."""
        return self in {ActionOutcome.PENDING_APPROVAL, ActionOutcome.HANDED_TO_HUMAN}


@dataclass(frozen=True)
class ActionRef:
    name: str  # the tool's name, for example "etsy.inventory.update"
    version: int


@dataclass(frozen=True)
class ActionRequest:
    action: ActionRef
    actor: ActorRef  # from the authenticated context or the execution, never from model output
    organization_id: uuid.UUID
    idempotency_key: str  # deterministic per business effect
    correlation_id: str
    input: Mapping[str, JsonValue] = field(default_factory=dict)
    shop_id: uuid.UUID | None = None
    causation_id: str | None = None
    on_behalf_of_user: uuid.UUID | None = None
    channel_hint: Channel | None = None
    deadline: datetime | None = None
    approval_id: uuid.UUID | None = None
    owner_trigger: bool = False

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip() or len(self.idempotency_key) > MAX_KEY_LENGTH:
            raise ValueError(f"an action's idempotency key is 1 to {MAX_KEY_LENGTH} characters")


@dataclass(frozen=True)
class ActionResult:
    execution_id: uuid.UUID
    outcome: ActionOutcome
    attempts: int
    channel: Channel | None = None
    output: Mapping[str, JsonValue] | None = None
    verified: bool = False
    error_code: str | None = None  # stable codes: "scope_missing", "capability_blocked", ...
    cost: Decimal | None = None
    retry_at: datetime | None = None  # DEFERRED: when a resubmission may run (slice 5.15)


class ActionGateway(Protocol):
    async def submit(self, request: ActionRequest) -> ActionResult:
        """The only entry point for business actions; idempotent per (action, key)."""
        ...


class UnavailableGateway:
    """Refuses every action until the gateway exists (slice 5.12)."""

    async def submit(self, request: ActionRequest) -> ActionResult:
        return ActionResult(
            execution_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"{request.action.name}:{request.idempotency_key}"
            ),
            outcome=ActionOutcome.DENIED,
            attempts=0,
            error_code="gateway_unavailable",
        )
