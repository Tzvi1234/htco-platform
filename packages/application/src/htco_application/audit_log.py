"""Reading the audit log (AC-3.9.1, FR-AUD-01): the Owner's and Admins' audit view.

`read_audit_log` answers one page of the organization's records, newest first, filtered by shop,
actor, action and time. Only organization-wide holders of `view_audit_log` (Owner and Admin) read
it, and the store reads as the caller, so the table's own policy (Owner and Admin only) decides
the rows as well.

Records are written with identifiers only (no buyer data, no secrets: every writer's rule). The
read keeps that promise even if a writer slipped: before and after values under a sensitive key
(a token, a secret, a password, an address, a buyer's name, a tracking code…) and any text shaped
like an email address are replaced by `REDACTED` before they leave the application.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind

MAX_PAGE = 100
REDACTED = "<redacted>"
# A key holding any of these (lower case) never shows its value.
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "verifier",
    "authorization",
    "cookie",
    "api_key",
    "email",
    "phone",
    "address",
    "buyer_name",
    "tracking_code",
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


@dataclass(frozen=True)
class AuditQuery:
    organization_id: uuid.UUID
    # The shop itself, or any record whose before or after names it as `shop_id`.
    shop_id: uuid.UUID | None = None
    actor_kind: ActorKind | None = None
    actor_id: str | None = None
    # An action (`membership.role_changed`), or a family ending in `*` (`membership.*`).
    action: str | None = None
    # `since` inclusive, `until` exclusive.
    since: datetime | None = None
    until: datetime | None = None
    # The cursor: records older than this one (IDs are time-ordered UUIDv7).
    before: uuid.UUID | None = None
    limit: int = 50


@dataclass(frozen=True)
class AuditEntry:
    id: uuid.UUID
    occurred_at: datetime
    actor_kind: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    before: Mapping[str, Any] | None = field(default=None, repr=False)
    after: Mapping[str, Any] | None = field(default=None, repr=False)
    correlation_id: str | None = None


@dataclass(frozen=True)
class AuditPage:
    items: tuple[AuditEntry, ...]
    # Pass back as `AuditQuery.before` for the next page; None on the last page.
    next_before: uuid.UUID | None


class AuditLogReader(Protocol):
    async def list_audit(self, claims: Mapping[str, Any], query: AuditQuery) -> AuditPage:
        """One page, newest first, read as the caller (RLS: Owner and Admin only); at most
        `query.limit` items, `next_before` set when more exist."""
        ...


class AuditForbiddenError(Exception):
    """Only the Owner and Admins read the organization's audit log."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"reading the audit log is not allowed ({outcome.value})")
        self.outcome = outcome


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    """The value with sensitive keys and email-shaped text redacted, recursively."""
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED if _sensitive(str(k)) and v is not None else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str) and _EMAIL.search(value):
        return REDACTED
    return value


def _redacted(entry: AuditEntry) -> AuditEntry:
    return AuditEntry(
        id=entry.id,
        occurred_at=entry.occurred_at,
        actor_kind=entry.actor_kind,
        actor_id=redact(entry.actor_id),
        action=entry.action,
        entity_type=entry.entity_type,
        entity_id=redact(entry.entity_id),
        before=None if entry.before is None else redact(entry.before),
        after=None if entry.after is None else redact(entry.after),
        correlation_id=entry.correlation_id,
    )


async def read_audit_log(
    auth: AuthContext,
    reader: AuditLogReader,
    query: AuditQuery,
    *,
    now: datetime,
) -> AuditPage:
    """Raises `OrganizationRequiredError`, `NoMembershipError` (not a member) and
    `AuditForbiddenError` (not the Owner or an Admin)."""
    principal = auth.principal_for(query.organization_id)
    decision = decide(principal, Capability.VIEW_AUDIT_LOG, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW:
        raise AuditForbiddenError(decision.outcome)
    if decision.scope is not Scope.ORGANIZATION:
        raise AuditForbiddenError(Outcome.DENY)
    limit = max(1, min(query.limit, MAX_PAGE))
    page = await reader.list_audit(
        auth.claims,
        AuditQuery(
            organization_id=principal.organization_id, shop_id=query.shop_id,
            actor_kind=query.actor_kind, actor_id=query.actor_id, action=query.action,
            since=query.since, until=query.until, before=query.before, limit=limit,
        ),
    )  # fmt: skip
    return AuditPage(items=tuple(_redacted(e) for e in page.items), next_before=page.next_before)


def organization_of(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    """The named organization, or the caller's only one."""
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return organization
