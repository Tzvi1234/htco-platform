"""The audit view (AC-3.9.1) on an in-memory reader: who reads it, the filters handed to the
store, the page size and the redaction of anything sensitive (invented values only)."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.access.authentication import NoMembershipError
from htco_application.audit_log import (
    MAX_PAGE,
    REDACTED,
    AuditEntry,
    AuditForbiddenError,
    AuditPage,
    AuditQuery,
    organization_of,
    read_audit_log,
    redact,
)
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind
from htco_domain.identity import Role
from tests.application.test_shop_dashboard import ORG, OTHER_ORG, SHOP, auth

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
USER_ID = "b0000000-0000-0000-0000-0000000000b1"


def entry(n: int, **changes: Any) -> AuditEntry:
    values: dict[str, Any] = {
        "id": uuid.UUID(f"01a00000-0000-7000-8000-{n:012d}"),
        "occurred_at": NOW - timedelta(minutes=n),
        "actor_kind": "user",
        "actor_id": USER_ID,
        "action": "membership.role_changed",
        "entity_type": "membership",
        "entity_id": str(uuid.UUID(int=n)),
        "before": {"role": "viewer"},
        "after": {"role": "operations"},
        "correlation_id": f"req-{n}",
    }
    return AuditEntry(**(values | changes))


@dataclass
class MemoryAuditLog:
    entries: list[AuditEntry] = field(default_factory=list)
    queries: list[AuditQuery] = field(default_factory=list)
    claims: list[Mapping[str, Any]] = field(default_factory=list)

    async def list_audit(self, claims: Mapping[str, Any], query: AuditQuery) -> AuditPage:
        self.claims.append(claims)
        self.queries.append(query)
        found = sorted(self.entries, key=lambda e: e.id, reverse=True)
        if query.before is not None:
            found = [e for e in found if e.id < query.before]
        page = tuple(found[: query.limit])
        more = len(found) > query.limit
        return AuditPage(items=page, next_before=page[-1].id if more else None)


@pytest.mark.ac("AC-3.9.1")
@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_the_owner_and_admins__read_the_organizations_records(role: Role) -> None:
    log = MemoryAuditLog([entry(1), entry(2)])

    page = await read_audit_log(auth(role), log, AuditQuery(ORG), now=NOW)

    assert [e.action for e in page.items] == ["membership.role_changed"] * 2
    assert page.next_before is None
    assert log.queries[0].organization_id == ORG


@pytest.mark.ac("AC-3.9.2")
@pytest.mark.parametrize(
    "role", [Role.DEVELOPER, Role.OPERATIONS, Role.FINANCE, Role.SUPPORT, Role.VIEWER]
)
async def test_other_roles__refused_and_nothing_read(role: Role) -> None:
    log = MemoryAuditLog([entry(1)])

    with pytest.raises(AuditForbiddenError):
        await read_audit_log(auth(role), log, AuditQuery(ORG), now=NOW)

    assert log.queries == []


async def test_another_organization__not_a_member() -> None:
    with pytest.raises(NoMembershipError):
        await read_audit_log(auth(), MemoryAuditLog(), AuditQuery(OTHER_ORG), now=NOW)


async def test_every_filter__reaches_the_store_with_a_bounded_page() -> None:
    log = MemoryAuditLog()
    query = AuditQuery(
        ORG, shop_id=SHOP, actor_kind=ActorKind.AUTOMATION, actor_id="tracking-sync",
        action="shop.*", since=NOW - timedelta(days=1), until=NOW, limit=10_000,
    )  # fmt: skip

    await read_audit_log(auth(), log, query, now=NOW)

    assert log.queries == [replace(query, limit=MAX_PAGE)]


async def test_pages__follow_the_cursor_newest_first() -> None:
    log = MemoryAuditLog([entry(n) for n in range(1, 6)])

    first = await read_audit_log(auth(), log, AuditQuery(ORG, limit=2), now=NOW)
    second = await read_audit_log(
        auth(), log, AuditQuery(ORG, limit=2, before=first.next_before), now=NOW
    )

    assert [uuid.UUID(e.entity_id).int for e in first.items] == [5, 4]
    assert [uuid.UUID(e.entity_id).int for e in second.items] == [3, 2]
    assert second.next_before is not None


@pytest.mark.ac("AC-3.9.1")
async def test_sensitive_values__redacted_never_returned() -> None:
    leaked = entry(
        1,
        before={"refresh_token": "rt-invented", "nested": {"api_key": "k-invented"}},
        after={
            "invited": "someone@example.test",
            "shipping_address": {"line1": "1 Invented St"},
            "tracking_code": "9400INVENTED",
            "role": "admin",
            "items": [{"buyer_name": "Invented Person"}, 7],
            "error": None,
        },
    )

    page = await read_audit_log(auth(), MemoryAuditLog([leaked]), AuditQuery(ORG), now=NOW)

    [found] = page.items
    assert found.before == {"refresh_token": REDACTED, "nested": {"api_key": REDACTED}}
    assert found.after == {
        "invited": REDACTED,
        "shipping_address": REDACTED,
        "tracking_code": REDACTED,
        "role": "admin",
        "items": [{"buyer_name": REDACTED}, 7],
        "error": None,
    }


def test_redact__identifiers_and_numbers_kept() -> None:
    kept = {"shop_id": str(SHOP), "receipt_id": 51, "last4": "1234", "state": "dead"}

    assert redact(kept) == kept


def test_organization_of__the_named_one_or_the_only_one() -> None:
    assert organization_of(auth(), None) == ORG
    assert organization_of(auth(), OTHER_ORG) == OTHER_ORG
    with pytest.raises(OrganizationRequiredError):
        organization_of(replace(auth(), memberships=()), None)
