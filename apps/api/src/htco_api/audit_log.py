"""The audit view (AC-3.9.1): `GET /v1/audit-log`.

The Owner and Admins read their organization's audit records, newest first, in cursor pages,
filtered by shop, actor, action (or an action family, `membership.*`) and time range. Records hold
identifiers only; values under sensitive keys and email-shaped text are redacted anyway. Answers
are `no-store`.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import (
    ORGANIZATION_REQUIRED,
    decode_cursor,
    encode_cursor,
    problem_responses,
)
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType, ValidationProblem
from htco_application.access.authentication import NoMembershipError
from htco_application.audit_log import (
    AuditEntry,
    AuditForbiddenError,
    AuditLogReader,
    AuditQuery,
    organization_of,
    read_audit_log,
)
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind

AUDIT_UNAVAILABLE = ProblemType(
    "audit-unavailable",
    "Audit log not available",
    503,
    "The audit log cannot be read on this server.",
)
# Actions are dotted lower-case names; a trailing `*` asks for the family.
_ACTION = r"^[a-z0-9_.]{1,64}\*?$"


class AuditEntryView(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    # user, employee, automation, advisor or system, and its ID (a user's UUID, an automation ID…)
    actor_kind: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    # What changed, as recorded (identifiers only; sensitive values show as "<redacted>").
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    correlation_id: str | None


class AuditLogPage(BaseModel):
    items: list[AuditEntryView]
    # Pass it back as `cursor` for the next page; null on the last page.
    next_cursor: str | None


def _reader(request: Request) -> AuditLogReader:
    found = getattr(request.app.state, "audit_log", None)
    if found is None:
        raise ProblemError(AUDIT_UNAVAILABLE)
    return found  # type: ignore[no-any-return]


def _view(entry: AuditEntry) -> AuditEntryView:
    return AuditEntryView(
        id=entry.id, occurred_at=entry.occurred_at, actor_kind=entry.actor_kind,
        actor_id=entry.actor_id, action=entry.action, entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        before=None if entry.before is None else dict(entry.before),
        after=None if entry.after is None else dict(entry.after),
        correlation_id=entry.correlation_id,
    )  # fmt: skip


def _invalid_range() -> RequestValidationError:
    return RequestValidationError(
        [{"loc": ("query", "until"), "msg": "until must be after since", "type": "value_error"}]
    )


def audit_log_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["audit"])

    @router.get(
        "/audit-log",
        summary="The organization's audit records (Owner and Admins)",
        responses=problem_responses(
            {
                400: "The caller belongs to several organizations and named none",
                403: "Only the Owner and Admins read the audit log (forbidden)",
                404: "The named organization does not exist for the caller",
                422: ("Invalid filter, time range, limit or cursor", ValidationProblem),
                503: "Not available on this server (audit-unavailable)",
            }
        ),
    )
    async def list_audit_log(  # noqa: PLR0913, PLR0917 - one query parameter per filter
        response: Response,
        auth: CurrentAuth,
        reader: Annotated[AuditLogReader, Depends(_reader)],
        organization_id: uuid.UUID | None = None,
        shop_id: uuid.UUID | None = None,
        actor_kind: ActorKind | None = None,
        actor_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        action: Annotated[str | None, Query(pattern=_ACTION)] = None,
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
    ) -> AuditLogPage:
        """Newest first; `since` is inclusive and `until` exclusive; `shop_id` matches the shop's
        own records and those naming it; pass `next_cursor` back as `cursor` for the next page."""
        before = decode_cursor(cursor) if cursor is not None else None
        if since is not None and until is not None and until <= since:
            raise _invalid_range()
        try:
            query = AuditQuery(
                organization_id=organization_of(auth, organization_id), shop_id=shop_id,
                actor_kind=actor_kind, actor_id=actor_id, action=action, since=since,
                until=until, before=before, limit=limit,
            )  # fmt: skip
            page = await read_audit_log(auth, reader, query, now=clock())
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError:
            raise ProblemError(NOT_FOUND) from None
        except AuditForbiddenError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return AuditLogPage(
            items=[_view(e) for e in page.items],
            next_cursor=None if page.next_before is None else encode_cursor(page.next_before),
        )

    return router
