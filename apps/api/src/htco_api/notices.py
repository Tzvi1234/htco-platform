"""Security notices from the API's routes (slice 6.4, AC-6.4.4): a sign-in from a new device, a
change of two-step verification, a shop connected or disconnected. Each is requested after the
action succeeded and never changes its answer: without the notices (tests, a server without a
database) nothing is requested."""

import uuid
from datetime import datetime

from fastapi import Request

from htco_application.notifications import SecurityNotices
from htco_domain.notifications import NotificationKind


def _notices(request: Request) -> SecurityNotices | None:
    found = getattr(request.app.state, "security_notices", None)
    return found if isinstance(found, SecurityNotices) else None


async def signed_in(
    request: Request, user_id: uuid.UUID, session_id: uuid.UUID, user_agent: str | None
) -> None:
    notices = _notices(request)
    if notices is not None:
        await notices.signed_in(user_id, session_id=session_id, user_agent=user_agent)


async def mfa_enrolled(request: Request, user_id: uuid.UUID, factor_id: str) -> None:
    notices = _notices(request)
    if notices is not None:
        await notices.mfa_changed(user_id, factor_id=factor_id, change="enrolled")


async def mfa_removed(request: Request, user_id: uuid.UUID, factor_id: str) -> None:
    notices = _notices(request)
    if notices is not None:
        await notices.mfa_changed(user_id, factor_id=factor_id, change="removed")


async def shop_changed(
    request: Request,
    kind: NotificationKind,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    *,
    at: datetime,
) -> None:
    notices = _notices(request)
    if notices is not None:
        await notices.shop(kind, organization_id, shop_id, key=at.strftime("%Y%m%dT%H%M%S"))
