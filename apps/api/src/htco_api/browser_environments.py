"""The shop's browser environment, read only (slice 5.16, ADR-0020).

`GET /v1/shops/{shop_id}/browser-environment` returns the status only, for the Owner and Admins
of the shop's organization. A shop without an environment is `not_configured`. There is no
creation endpoint: creating an environment is Phase 8A's, behind the `browser_environments`
flag.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType, ValidationProblem
from htco_application.browser_environments import EnvironmentReader, browser_environment_status
from htco_application.kernel import ShopOwners
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError

BROWSER_ENVIRONMENTS_OFF = ProblemType(
    "browser-environments-unavailable",
    "Browser environments not available",
    503,
    "Browser environments are not set up on this server.",
)


@dataclass(frozen=True)
class BrowserEnvironmentServices:
    reader: EnvironmentReader
    shops: ShopOwners


class BrowserEnvironmentView(BaseModel):
    shop_id: uuid.UUID
    # not_configured, provisioning, ready, login_required, degraded, suspended or failed
    status: str


def _services(request: Request) -> BrowserEnvironmentServices:
    found = getattr(request.app.state, "browser_environments", None)
    if not isinstance(found, BrowserEnvironmentServices):
        raise ProblemError(BROWSER_ENVIRONMENTS_OFF)
    return found


_Services = Annotated[BrowserEnvironmentServices, Depends(_services)]


def browser_environments_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1/shops/{shop_id}", tags=["browser environments"])

    @router.get(
        "/browser-environment",
        summary="The status of the shop's browser environment",
        responses=problem_responses(
            {
                403: "Only the Owner and Admins see the browser environment (forbidden)",
                404: "No such shop for the caller",
                422: ("Invalid shop ID", ValidationProblem),
                503: "Browser environments are not set up on this server "
                "(browser-environments-unavailable)",
            }
        ),
    )
    async def get_browser_environment(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, services: _Services
    ) -> BrowserEnvironmentView:
        """The status only; never the profile, the consent or any credential."""
        try:
            status = await anyio.to_thread.run_sync(
                lambda: browser_environment_status(
                    auth, services.reader, services.shops, shop_id, now=clock()
                )
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return BrowserEnvironmentView(shop_id=shop_id, status=status.value)

    return router
