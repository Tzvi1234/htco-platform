"""The failed-email admin view (AC-6.4.3): emails the provider refused for good, or that were
skipped, for the Owner and Admins.

A permanent failure is recorded once and never retried (slice 6.4); this is where it becomes
visible: its kind, status, reason (a status code or a type name), time, and what it was about (the
shop, the incident and the order). The recipient is shown as a member ID and a masked address
(`o***@e***.test`), masked by the database; the full address never reaches the application.
The store reads as the caller, so the table's own policy (Owner and Admin) decides the rows too.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide
from htco_application.shops import OrganizationRequiredError

MAX_PAGE = 100


class FailedStatus(StrEnum):
    FAILED = "failed"  # the provider refused it (permanent)
    SKIPPED = "skipped"  # never sent: the member had left


@dataclass(frozen=True)
class FailedEmail:
    id: uuid.UUID
    kind: str
    status: FailedStatus
    reason: str | None
    created_at: datetime
    recipient_user_id: uuid.UUID
    # Masked by the database; None when the member is gone.
    recipient: str | None
    shop_id: uuid.UUID | None
    incident_id: uuid.UUID | None
    receipt_id: int | None


@dataclass(frozen=True)
class FailedEmailPage:
    items: tuple[FailedEmail, ...]
    next_before: uuid.UUID | None


class FailedEmailReader(Protocol):
    async def failed_emails(
        self,
        claims: Mapping[str, Any],
        organization_id: uuid.UUID,
        *,
        status: FailedStatus,
        before: uuid.UUID | None,
        limit: int,
    ) -> FailedEmailPage:
        """Newest first, read as the caller (RLS), at most `limit`; `next_before` when more."""
        ...


class EmailFailuresForbiddenError(Exception):
    """Only the Owner and Admins see the email deliveries."""


async def failed_emails(  # noqa: PLR0913 - the caller, the reader and the page
    auth: AuthContext,
    reader: FailedEmailReader,
    *,
    organization_id: uuid.UUID | None,
    status: FailedStatus = FailedStatus.FAILED,
    before: uuid.UUID | None = None,
    limit: int = 50,
    now: datetime,
) -> FailedEmailPage:
    """Raises `OrganizationRequiredError`, `NoMembershipError` and
    `EmailFailuresForbiddenError` (the rule of the rest of Admin > Health)."""
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)
    decision = decide(principal, Capability.RETRY_SYNC, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW or decision.scope is not Scope.ORGANIZATION:
        raise EmailFailuresForbiddenError(decision.outcome)
    return await reader.failed_emails(
        auth.claims,
        principal.organization_id,
        status=status,
        before=before,
        limit=max(1, min(limit, MAX_PAGE)),
    )
