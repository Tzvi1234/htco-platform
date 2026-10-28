"""Versioned policies and recorded decisions (slice 7.2, AC-7.2.3).

- **Publishing** (`PolicyService.publish`): a change is the next version of the organization's
  policy, never an edit, and is audited in the same transaction. Only the Owner at `aal2` with a
  recent step-up may publish ("Edit model policies, budgets, kill switches"). Reverting is
  publishing an older document again, as a new version.
- **The baseline** (`ensure_baseline`): version 1 from the registry, published once by the
  platform when the organization has no policy yet.
- **Deciding** (`decide`): the engine's answer under the latest version, recorded with that
  version in `app.policy_decisions`, so an old decision keeps the version it was made under.

A recorded decision holds identifiers, the capability, the reason and the version only: never
request parameters or buyer data.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.engine import PolicyRequest, evaluate
from htco_application.access.policy import Capability, Outcome, decide
from htco_application.audit import AuditRecord
from htco_application.kernel import Offload, run_inline
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.policy import PolicyDecision, PolicyDocument, PolicyVersion, Reason

# The service is the engine's only entry point (tests/architecture/test_policy_engine_entry.py),
# so it re-exports the request callers build (quill2 on #189).
__all__ = [
    "BASELINE_ACTOR",
    "MAX_CORRELATION_ID",
    "POLICY_PUBLISHED",
    "PolicyEditDeniedError",
    "PolicyRequest",
    "PolicyService",
    "PolicyStore",
    "RecordedDecision",
]

POLICY_PUBLISHED = "policies.published"
BASELINE_ACTOR = ActorRef(ActorKind.SYSTEM, "policy-baseline")
MAX_CORRELATION_ID = 200


class PolicyEditDeniedError(Exception):
    """The caller may not publish a policy change; `reason` says why (MFA, step-up, role)."""

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__(f"publishing a policy is denied: {reason.value}")


@dataclass(frozen=True)
class RecordedDecision:
    """One row of `app.policy_decisions`."""

    id: uuid.UUID
    organization_id: uuid.UUID
    decision: PolicyDecision
    shop_id: uuid.UUID | None
    correlation_id: str
    decided_at: datetime


class PolicyStore(Protocol):
    """Synchronous; the service offloads it."""

    def latest(self, organization_id: uuid.UUID) -> PolicyVersion | None: ...

    def publish(  # noqa: PLR0913 - the version's columns and its audit record
        self,
        organization_id: uuid.UUID,
        document: PolicyDocument,
        *,
        created_by: ActorRef,
        now: datetime,
        audit: Callable[[int], AuditRecord],
        only_if_none: bool = False,
    ) -> PolicyVersion:
        """In one transaction, serialized per organization: insert the next version and
        `audit(version)`. With `only_if_none`, return the latest version instead when the
        organization already has one, and write nothing."""
        ...

    def record(self, decision: RecordedDecision) -> None: ...


_EDIT_DENIALS = {
    Outcome.DENY: Reason.NOT_GRANTED,
    Outcome.MFA_REQUIRED: Reason.MFA_REQUIRED,
    Outcome.STEP_UP_REQUIRED: Reason.STEP_UP_REQUIRED,
}


class PolicyService:
    def __init__(
        self,
        store: PolicyStore,
        *,
        ids: Callable[[], uuid.UUID] = uuid.uuid7,
        offload: Offload = run_inline,
    ) -> None:
        self._store = store
        self._ids = ids
        self._offload = offload

    async def active(self, organization_id: uuid.UUID) -> PolicyVersion | None:
        return await self._offload(lambda: self._store.latest(organization_id))

    async def ensure_baseline(
        self, organization_id: uuid.UUID, document: PolicyDocument, *, now: datetime
    ) -> PolicyVersion:
        return await self._publish(
            organization_id, document, BASELINE_ACTOR, now, correlation_id=None, only_if_none=True
        )

    async def publish(
        self,
        context: AuthContext,
        organization_id: uuid.UUID,
        document: PolicyDocument,
        *,
        now: datetime,
        correlation_id: str | None = None,
    ) -> PolicyVersion:
        if now.tzinfo is None:
            raise ValueError("publish needs a timezone-aware clock")
        try:
            principal = context.principal_for(organization_id)
        except NoMembershipError:
            raise PolicyEditDeniedError(Reason.UNKNOWN_ACTOR) from None
        outcome = decide(principal, Capability.EDIT_MODEL_POLICIES, shop=None, now=now).outcome
        if outcome is not Outcome.ALLOW:
            raise PolicyEditDeniedError(_EDIT_DENIALS[outcome])
        editor = ActorRef(ActorKind.USER, str(context.user_id))
        return await self._publish(
            organization_id, document, editor, now, correlation_id=correlation_id
        )

    async def _publish(  # noqa: PLR0913 - the version's columns
        self,
        organization_id: uuid.UUID,
        document: PolicyDocument,
        created_by: ActorRef,
        now: datetime,
        *,
        correlation_id: str | None,
        only_if_none: bool = False,
    ) -> PolicyVersion:
        def audit(version: int) -> AuditRecord:
            return AuditRecord(
                id=self._ids(),
                organization_id=organization_id,
                actor=created_by,
                action=POLICY_PUBLISHED,
                entity_type="policy",
                entity_id=str(version),
                before=None if version == 1 else {"version": version - 1},
                after={"version": version},
                correlation_id=correlation_id,
            )

        return await self._offload(
            lambda: self._store.publish(
                organization_id,
                document,
                created_by=created_by,
                now=now,
                audit=audit,
                only_if_none=only_if_none,
            )
        )

    async def decide(self, request: PolicyRequest, *, correlation_id: str) -> PolicyDecision:
        """The engine's answer under the organization's latest policy, recorded with its
        version before it is returned."""
        policy = await self.active(request.organization_id)
        decision = evaluate(policy, request)
        # Only a shop of this organization is recorded (Codex on #189): a request naming another
        # tenant's shop is refused, and its trail keeps the reason, never that tenant's shop ID.
        shop = request.shop
        own_shop = shop is not None and shop.organization_id == request.organization_id
        recorded = RecordedDecision(
            id=self._ids(),
            organization_id=request.organization_id,
            decision=decision,
            shop_id=shop.id if own_shop and shop is not None else None,
            correlation_id=correlation_id[:MAX_CORRELATION_ID],
            decided_at=request.now,
        )
        await self._offload(lambda: self._store.record(recorded))
        return decision
