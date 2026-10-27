"""The shop's browser environment (slice 5.16, ADR-0020) and its configuration profile (slice
8A.2b, ADR-0025 §2).

- `GET /v1/shops/{shop_id}/browser-environment` returns the status only, for the Owner and Admins
  of the shop's organization. A shop without an environment is `not_configured`. There is no
  creation endpoint yet: creating an environment is Phase 8A's, behind the `browser_environments`
  flag.
- `GET /v1/shops/{shop_id}/browser-environment/configuration` returns the current version of the
  configuration profile (the locale and the egress) with the platform baseline, for the Owner
  and Admins.
- `PUT /v1/shops/{shop_id}/browser-environment/configuration` saves a new version: the Owner or an
  Admin, with MFA and a step-up in the last ten minutes; only the Owner changes the egress. The
  body names the version it edits (`expected_version`, 0 before the first). A refusal names each
  issue as a code and the setting it concerns, never the refused value; nothing is saved. Each
  accepted change is audited with the request ID as its correlation ID.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from htco_api.auth import CurrentAuth
from htco_api.identity import problem_responses
from htco_api.policy_guard import FORBIDDEN, MFA_REQUIRED, STEP_UP_REQUIRED
from htco_api.problems import (
    NOT_FOUND,
    FieldError,
    Problem,
    ProblemError,
    ProblemType,
    ValidationProblem,
    problem_response,
    request_id_of,
)
from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Outcome
from htco_application.browser_configuration import (
    BrowserConfigurationForbiddenError,
    ConfigurationServices,
    ShopLanguagesUnavailableError,
    StaleConfigurationError,
    browser_configuration,
    save_browser_configuration,
)
from htco_application.browser_environments import (
    EnvironmentNotFoundError,
    EnvironmentReader,
    browser_environment_status,
)
from htco_application.kernel import ShopOwners
from htco_application.shop_capabilities import ShopNotConnectedError
from htco_application.shop_dashboard import (
    ShopDashboard,
    ShopDataScopeMissingError,
    ShopDataUnavailableError,
    shop_languages,
)
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.browser_configuration import (
    PLATFORM_BASELINE,
    ConfigurationRefusedError,
    ConfigurationVersion,
)

BROWSER_ENVIRONMENTS_OFF = ProblemType(
    "browser-environments-unavailable",
    "Browser environments not available",
    503,
    "Browser environments are not set up on this server.",
)
BROWSER_CONFIGURATION_OFF = ProblemType(
    "browser-configuration-unavailable",
    "Browser configuration not available",
    503,
    "Browser configuration profiles are not set up on this server.",
)
ENVIRONMENT_NOT_CONFIGURED = ProblemType(
    "browser-environment-not-configured",
    "No browser environment",
    404,
    "This shop has no browser environment.",
)
CONFIGURATION_INVALID = ProblemType(
    "browser-configuration-invalid",
    "Configuration refused",
    422,
    "The configuration was not saved. Each issue names the setting it concerns.",
)
CONFIGURATION_STALE = ProblemType(
    "browser-configuration-stale",
    "Configuration changed meanwhile",
    409,
    "Someone saved another version meanwhile. Reload it and edit again.",
)
SHOP_LANGUAGES_UNAVAILABLE = ProblemType(
    "shop-languages-unavailable",
    "Shop languages unavailable",
    503,
    "The shop's languages could not be read from the marketplace now. Try again shortly.",
)
_PROBLEM_FOR = {
    Outcome.DENY: FORBIDDEN,
    Outcome.MFA_REQUIRED: MFA_REQUIRED,
    Outcome.STEP_UP_REQUIRED: STEP_UP_REQUIRED,
}


@dataclass(frozen=True)
class BrowserEnvironmentServices:
    reader: EnvironmentReader
    shops: ShopOwners
    # None until the marketplace reads are wired (the shop's languages come from them).
    configuration: ConfigurationServices | None = None


class DashboardShopLanguages:
    """The shop's recorded languages, from its marketplace profile through the dashboard's
    cached read (a GET only). A shop that is not connected, or a read that fails, saves nothing."""

    def __init__(self, dashboard: ShopDashboard, clock: Callable[[], datetime]) -> None:
        self._dashboard = dashboard
        self._clock = clock

    async def recorded(self, auth: AuthContext, shop_id: uuid.UUID) -> tuple[str, ...]:
        try:
            return await shop_languages(self._dashboard, auth, shop_id, now=self._clock())
        except ShopNotConnectedError, ShopDataUnavailableError, ShopDataScopeMissingError:
            raise ShopLanguagesUnavailableError from None


class BrowserEnvironmentView(BaseModel):
    shop_id: uuid.UUID
    # not_configured, provisioning, ready, login_required, degraded, suspended or failed
    status: str


class ViewportView(BaseModel):
    width: int
    height: int


class BaselineView(BaseModel):
    """The platform baseline: the same for every shop and never editable."""

    viewport: ViewportView
    color_scheme: str
    denied_permissions: list[str]


class BrowserConfigurationView(BaseModel):
    shop_id: uuid.UUID
    # 0 before the first save; the next PUT names it as `expected_version`.
    version: int
    locale: str | None
    egress_id: str | None
    # The shop's recorded languages at the save: the locale is one of them.
    shop_languages: list[str]
    created_at: datetime | None
    baseline: BaselineView


class BrowserConfigurationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, le=1_000_000)
    # `locale` and, for the Owner, `egress_id`. Any other key is refused by name.
    settings: dict[str, Any] = Field(max_length=20)


class ConfigurationIssueView(BaseModel):
    code: str
    field: str


class ConfigurationRefusedProblem(Problem):
    """`browser-configuration-invalid` names its `issues`; a malformed request is a
    `validation-error` with `errors` instead, on the same status."""

    issues: list[ConfigurationIssueView] | None = None
    errors: list[FieldError] | None = None


class StaleConfigurationProblem(Problem):
    current_version: int


def _services(request: Request) -> BrowserEnvironmentServices:
    found = getattr(request.app.state, "browser_environments", None)
    if not isinstance(found, BrowserEnvironmentServices):
        raise ProblemError(BROWSER_ENVIRONMENTS_OFF)
    return found


_Services = Annotated[BrowserEnvironmentServices, Depends(_services)]


def _configuration(services: BrowserEnvironmentServices) -> ConfigurationServices:
    if services.configuration is None:
        raise ProblemError(BROWSER_CONFIGURATION_OFF)
    return services.configuration


_BASELINE = BaselineView(
    viewport=ViewportView(
        width=PLATFORM_BASELINE.viewport.width, height=PLATFORM_BASELINE.viewport.height
    ),
    color_scheme=PLATFORM_BASELINE.color_scheme,
    denied_permissions=sorted(PLATFORM_BASELINE.denied_permissions),
)


def _view(shop_id: uuid.UUID, found: ConfigurationVersion | None) -> BrowserConfigurationView:
    if found is None:
        return BrowserConfigurationView(
            shop_id=shop_id, version=0, locale=None, egress_id=None, shop_languages=[],
            created_at=None, baseline=_BASELINE,
        )  # fmt: skip
    return BrowserConfigurationView(
        shop_id=shop_id, version=found.version, locale=found.settings.locale,
        egress_id=found.settings.egress_id, shop_languages=list(found.shop_languages),
        created_at=found.created_at, baseline=_BASELINE,
    )  # fmt: skip


def _refused(request: Request, refused: ConfigurationRefusedError) -> JSONResponse:
    base = CONFIGURATION_INVALID.problem(request_id_of(request)).model_dump()
    issues = [ConfigurationIssueView(code=i.code, field=i.field) for i in refused.issues]
    return problem_response(ConfigurationRefusedProblem(**base, issues=issues))


def _stale(request: Request, stale: StaleConfigurationError) -> JSONResponse:
    base = CONFIGURATION_STALE.problem(request_id_of(request)).model_dump()
    return problem_response(
        StaleConfigurationProblem(**base, current_version=stale.current_version)
    )


_COMMON: dict[int, str | tuple[str, type[Problem]]] = {
    404: "No such shop for the caller, or the shop has no browser environment "
    "(not-found, browser-environment-not-configured)",
    503: "Browser environments or their configuration are not set up on this server "
    "(browser-environments-unavailable, browser-configuration-unavailable)",
}


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

    @router.get(
        "/browser-environment/configuration",
        summary="The shop's browser configuration profile",
        responses=problem_responses(
            {
                **_COMMON,
                403: "Only the Owner and Admins see the configuration (forbidden)",
                422: ("Invalid shop ID", ValidationProblem),
            }
        ),
    )
    async def get_browser_configuration(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, services: _Services
    ) -> BrowserConfigurationView:
        """The current version (version 0 before the first save) and the platform baseline."""
        configuration = _configuration(services)
        try:
            found = await browser_configuration(auth, configuration, shop_id, now=clock())
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except BrowserConfigurationForbiddenError as refused:
            raise ProblemError(_PROBLEM_FOR[refused.outcome]) from None
        except EnvironmentNotFoundError:
            raise ProblemError(ENVIRONMENT_NOT_CONFIGURED) from None
        response.headers["Cache-Control"] = "no-store"
        return _view(shop_id, found)

    @router.put(
        "/browser-environment/configuration",
        summary="Save a new version of the shop's browser configuration profile",
        response_model=BrowserConfigurationView,
        responses=problem_responses(
            {
                **_COMMON,
                400: "The body is not valid JSON",
                403: "Not the Owner or an Admin, no second factor, or no recent step-up "
                "(forbidden, mfa-required, step-up-required), or the request did not come "
                "from the web app (csrf-failed)",
                409: (
                    "Another version was saved meanwhile (browser-configuration-stale)",
                    StaleConfigurationProblem,
                ),
                422: (
                    "The configuration is refused, each issue named "
                    "(browser-configuration-invalid), or the request is malformed",
                    ConfigurationRefusedProblem,
                ),
                503: "Not set up on this server, or the shop's languages cannot be read now "
                "(browser-environments-unavailable, browser-configuration-unavailable, "
                "shop-languages-unavailable)",
            }
        ),
    )
    async def put_browser_configuration(  # noqa: PLR0913 - the path, the body and the request
        shop_id: uuid.UUID,
        *,
        body: BrowserConfigurationEdit,
        request: Request,
        response: Response,
        auth: CurrentAuth,
        services: _Services,
    ) -> Any:
        """Validated before saving; the egress is the Owner's to change. Audited."""
        configuration = _configuration(services)
        try:
            saved = await save_browser_configuration(
                auth, configuration, shop_id, settings=body.settings,
                expected_version=body.expected_version,
                correlation_id=request_id_of(request) or str(uuid.uuid7()), now=clock(),
            )  # fmt: skip
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except BrowserConfigurationForbiddenError as refused:
            raise ProblemError(_PROBLEM_FOR[refused.outcome]) from None
        except EnvironmentNotFoundError:
            raise ProblemError(ENVIRONMENT_NOT_CONFIGURED) from None
        except ShopLanguagesUnavailableError:
            raise ProblemError(SHOP_LANGUAGES_UNAVAILABLE) from None
        except StaleConfigurationError as stale:
            return _stale(request, stale)
        except ConfigurationRefusedError as refused:
            return _refused(request, refused)
        response.headers["Cache-Control"] = "no-store"
        return _view(shop_id, saved)

    return router
