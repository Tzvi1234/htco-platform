"""Audit records (slice 2.5, FR-AUD-01): who did what to which entity, before and after.

Records are written in the same transaction as the change they describe, so a change without its
record cannot be committed. The actor is an `ActorRef`: people, AI employees, automations, the
Decision Advisor and the platform itself all write the same shape (ADR-0012).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from htco_domain.actors import ActorRef


@dataclass(frozen=True)
class AuditRecord:
    id: uuid.UUID
    organization_id: uuid.UUID
    actor: ActorRef
    action: str
    entity_type: str
    entity_id: str
    before: Mapping[str, Any] | None = field(default=None, repr=False)
    after: Mapping[str, Any] | None = field(default=None, repr=False)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("action", "entity_type", "entity_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"audit {name} must not be blank")

    @classmethod
    def new(  # noqa: PLR0913 - one argument per column of the record
        cls,
        *,
        organization_id: uuid.UUID,
        actor: ActorRef,
        action: str,
        entity_type: str,
        entity_id: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AuditRecord:
        return cls(
            id=uuid.uuid7(),
            organization_id=organization_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            correlation_id=correlation_id,
        )


class AuditWriter(Protocol):
    """Appends records inside the caller's transaction."""

    def append_audit(self, record: AuditRecord) -> None: ...
