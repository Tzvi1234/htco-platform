"""The failed-email admin view (AC-6.4.3) on an in-memory reader: the Owner and Admins only, the
page bound and the status handed to the store (invented values only)."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.access.authentication import NoMembershipError
from htco_application.email_failures import (
    MAX_PAGE,
    EmailFailuresForbiddenError,
    FailedEmail,
    FailedEmailPage,
    FailedStatus,
    failed_emails,
)
from htco_domain.identity import Role
from tests.application.test_shop_dashboard import ORG, OTHER_ORG, SHOP, auth

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
MEMBER = uuid.UUID("b0000000-0000-0000-0000-0000000000c1")
INCIDENT = uuid.UUID("b0000000-0000-0000-0000-0000000000d1")


def failure(n: int, status: FailedStatus = FailedStatus.FAILED) -> FailedEmail:
    return FailedEmail(
        id=uuid.UUID(f"01a00000-0000-7000-8000-{n:012d}"), kind="incident_opened", status=status,
        reason="resend 422" if status is FailedStatus.FAILED else "no_member",
        created_at=NOW - timedelta(minutes=n), recipient_user_id=MEMBER,
        recipient="o***@e***.test", shop_id=SHOP, incident_id=INCIDENT, receipt_id=7,
    )  # fmt: skip


@dataclass
class MemoryFailedEmails:
    rows: list[FailedEmail] = field(default_factory=list)
    asked: list[tuple[uuid.UUID, FailedStatus, uuid.UUID | None, int]] = field(default_factory=list)

    async def failed_emails(
        self,
        claims: Mapping[str, Any],
        organization_id: uuid.UUID,
        *,
        status: FailedStatus,
        before: uuid.UUID | None,
        limit: int,
    ) -> FailedEmailPage:
        assert claims is not None
        self.asked.append((organization_id, status, before, limit))
        found = sorted((r for r in self.rows if r.status is status), key=lambda r: r.id)[::-1]
        if before is not None:
            found = [r for r in found if r.id < before]
        page = tuple(found[:limit])
        return FailedEmailPage(page, page[-1].id if len(found) > limit else None)


@pytest.mark.ac("AC-6.4.3")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_the_owner_and_admins__see_permanent_failures(role: Role) -> None:
    reader = MemoryFailedEmails([failure(1), failure(2), failure(3, FailedStatus.SKIPPED)])

    page = await failed_emails(auth(role), reader, organization_id=None, now=NOW)

    assert [r.id for r in page.items] == [failure(2).id, failure(1).id]
    assert reader.asked == [(ORG, FailedStatus.FAILED, None, 50)]


@pytest.mark.parametrize(
    "role", [Role.OPERATIONS, Role.DEVELOPER, Role.FINANCE, Role.SUPPORT, Role.VIEWER]
)
async def test_other_roles__refused_and_nothing_read(role: Role) -> None:
    reader = MemoryFailedEmails([failure(1)])

    with pytest.raises(EmailFailuresForbiddenError):
        await failed_emails(auth(role), reader, organization_id=None, now=NOW)

    assert reader.asked == []


async def test_skipped__and_a_bounded_page() -> None:
    reader = MemoryFailedEmails([failure(1, FailedStatus.SKIPPED)])

    page = await failed_emails(
        auth(), reader, organization_id=ORG, status=FailedStatus.SKIPPED, limit=10_000, now=NOW
    )

    assert [r.reason for r in page.items] == ["no_member"]
    assert reader.asked[0][3] == MAX_PAGE


async def test_another_organization__not_a_member() -> None:
    with pytest.raises(NoMembershipError):
        await failed_emails(auth(), MemoryFailedEmails(), organization_id=OTHER_ORG, now=NOW)
