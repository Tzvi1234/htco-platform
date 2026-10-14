"""Member and shop-access management endpoints (slice 2.6).

Owner and Admin, from an `aal2` session, list the organization's members, invite a user by email,
change a member's role, remove a member, and grant or revoke a member's access to a shop. Every
change is audited with the request ID as its correlation ID. Members and shops of another
organization answer 404; a caller outside the organization, or without the capability, 403.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from htco_api.auth import CurrentAuth
from htco_api.identity import FORBIDDEN, problem_responses
from htco_api.problems import (
    NOT_FOUND,
    NOT_READY,
    ProblemError,
    ProblemType,
    ValidationProblem,
    request_id_of,
)
from htco_application.access.management import (
    AccessDeniedError,
    AccessStore,
    GrantNotFoundError,
    InviteeAlreadyRegisteredError,
    Inviter,
    MemberAlreadyExistsError,
    MemberRow,
    MembershipNotFoundError,
    MfaRequiredError,
    ShopNotInOrganizationError,
    change_role,
    grant_shop,
    invite_member,
    list_members,
    remove_member,
    revoke_shop,
)
from htco_domain.identity import Role

MFA_REQUIRED = ProblemType(
    "mfa-required",
    "Second factor required",
    403,
    "Managing access needs a session signed in with your second factor.",
)
MEMBER_EXISTS = ProblemType(
    "member-exists", "Already a member", 409, "This user is already a member of the organization."
)
USER_ALREADY_REGISTERED = ProblemType(
    "user-already-registered",
    "Account already exists",
    409,
    "This email already has an HTCO account and cannot be invited again.",
)
INVITATIONS_UNAVAILABLE = ProblemType(
    "invitations-unavailable",
    "Invitations unavailable",
    503,
    "Invitations cannot be sent right now. Try again shortly.",
)


class InvitationFailedError(Exception):
    """Raised by the composition root's inviter when the sign-in service cannot invite."""


class MemberView(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    display_name: str | None
    role: Role
    shop_ids: list[uuid.UUID]


class MemberList(BaseModel):
    # All active members; an organization has a handful, far below any page size.
    items: list[MemberView]


class Invitation(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Role


class RoleChange(BaseModel):
    role: Role


def _view(member: MemberRow) -> MemberView:
    return MemberView(
        id=member.id,
        user_id=member.user_id,
        display_name=member.display_name,
        role=member.role,
        shop_ids=sorted(member.shop_ids),
    )


def _store(request: Request) -> AccessStore:
    store = getattr(request.app.state, "access", None)
    if store is None:
        raise ProblemError(NOT_READY)
    access: AccessStore = store
    return access


def _inviter(request: Request) -> Inviter:
    inviter = getattr(request.app.state, "inviter", None)
    if inviter is None:
        raise ProblemError(INVITATIONS_UNAVAILABLE)
    found: Inviter = inviter
    return found


@contextmanager
def _problems() -> Iterator[None]:
    """Turn the use cases' errors into problem responses."""
    try:
        yield
    except MfaRequiredError:
        raise ProblemError(MFA_REQUIRED) from None
    except AccessDeniedError:
        raise ProblemError(FORBIDDEN) from None
    except MembershipNotFoundError, ShopNotInOrganizationError, GrantNotFoundError:
        raise ProblemError(NOT_FOUND) from None
    except MemberAlreadyExistsError:
        raise ProblemError(MEMBER_EXISTS) from None
    except InviteeAlreadyRegisteredError:
        raise ProblemError(USER_ALREADY_REGISTERED) from None
    except InvitationFailedError:
        raise ProblemError(INVITATIONS_UNAVAILABLE) from None


def _correlation(request: Request) -> str:
    return request_id_of(request) or str(uuid.uuid7())


StoreDep = Annotated[AccessStore, Depends(_store)]


def access_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(
        prefix="/v1/organizations/{organization_id}",
        tags=["access"],
        responses=problem_responses(
            {
                403: "Not Owner or Admin of this organization, or the session lacks MFA",
                404: "No such member, shop or grant in this organization",
                422: ("Invalid path, body or role", ValidationProblem),
            }
        ),
    )

    @router.get("/members", summary="The organization's members")
    async def list_organization_members(
        organization_id: uuid.UUID, auth: CurrentAuth, store: StoreDep
    ) -> MemberList:
        """Active members with their role and granted shops. Owner and Admin, with MFA."""
        with _problems():
            found = await list_members(auth, store, organization_id, now=clock())
        return MemberList(items=[_view(member) for member in found])

    @router.post(
        "/invitations",
        status_code=status.HTTP_201_CREATED,
        summary="Invite a user by email",
        responses=problem_responses(
            {
                400: "The body is not valid JSON",
                409: "Already a member, or the email already has an account",
                422: ("Invalid email or role", ValidationProblem),
                503: "Invitations cannot be sent right now",
            }
        ),
    )
    async def invite_organization_member(  # noqa: PLR0913, PLR0917 - FastAPI passes each dependency as a parameter
        organization_id: uuid.UUID,
        body: Invitation,
        request: Request,
        auth: CurrentAuth,
        store: StoreDep,
        inviter: Annotated[Inviter, Depends(_inviter)],
    ) -> MemberView:
        """Sends the sign-in service's invitation and adds the membership. Only the Owner may
        invite another Owner."""
        try:
            with _problems():
                member = await invite_member(
                    auth,
                    store,
                    inviter,
                    organization_id,
                    email=body.email,
                    role=body.role,
                    now=clock(),
                    correlation_id=_correlation(request),
                )
        except ValueError:
            raise RequestValidationError(
                [{"loc": ("body", "email"), "msg": "Invalid email address", "type": "value_error"}]
            ) from None
        return _view(member)

    @router.patch(
        "/members/{membership_id}",
        summary="Change a member's role",
        responses=problem_responses(
            {400: "The body is not valid JSON", 422: ("Invalid role", ValidationProblem)}
        ),
    )
    async def change_member_role(  # noqa: PLR0913, PLR0917 - FastAPI passes each dependency as a parameter
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        body: RoleChange,
        request: Request,
        auth: CurrentAuth,
        store: StoreDep,
    ) -> MemberView:
        """The Owner's membership cannot be changed; only the Owner may grant the Owner role."""
        with _problems():
            member = await change_role(
                auth,
                store,
                organization_id,
                membership_id,
                body.role,
                now=clock(),
                correlation_id=_correlation(request),
            )
        return _view(member)

    @router.delete(
        "/members/{membership_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Remove a member",
        response_class=Response,
    )
    async def remove_organization_member(
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        store: StoreDep,
    ) -> None:
        """Takes effect on the member's next request. The Owner cannot be removed."""
        with _problems():
            await remove_member(
                auth,
                store,
                organization_id,
                membership_id,
                now=clock(),
                correlation_id=_correlation(request),
            )

    @router.put(
        "/members/{membership_id}/shops/{shop_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Grant a member access to a shop",
        response_class=Response,
    )
    async def grant_member_shop(  # noqa: PLR0913, PLR0917 - FastAPI passes each dependency as a parameter
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        shop_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        store: StoreDep,
    ) -> None:
        """Idempotent: granting an existing grant changes nothing."""
        with _problems():
            await grant_shop(
                auth,
                store,
                organization_id,
                membership_id,
                shop_id,
                now=clock(),
                correlation_id=_correlation(request),
            )

    @router.delete(
        "/members/{membership_id}/shops/{shop_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Revoke a member's access to a shop",
        response_class=Response,
    )
    async def revoke_member_shop(  # noqa: PLR0913, PLR0917 - FastAPI passes each dependency as a parameter
        organization_id: uuid.UUID,
        membership_id: uuid.UUID,
        shop_id: uuid.UUID,
        request: Request,
        auth: CurrentAuth,
        store: StoreDep,
    ) -> None:
        """Takes effect on the member's next request."""
        with _problems():
            await revoke_shop(
                auth,
                store,
                organization_id,
                membership_id,
                shop_id,
                now=clock(),
                correlation_id=_correlation(request),
            )

    return router
