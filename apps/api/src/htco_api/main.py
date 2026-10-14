"""Process entry point and composition root: `python -m htco_api`."""

import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import structlog
import uvicorn
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from htco_api.access import InvitationFailedError
from htco_api.app import AppOptions, Resource, create_app
from htco_api.auth import Authenticator
from htco_api.settings import ApiSettings, Environment
from htco_application.access.management import AccessStore, InviteeAlreadyRegisteredError
from htco_application.shops import ShopReader
from htco_persistence.access import PostgresAccessStore
from htco_persistence.database import Database
from htco_persistence.identity import PostgresMembershipReader
from htco_persistence.readiness import DatabaseCheck
from htco_persistence.shops import PostgresShopReader
from htco_platform.error_reporting import ErrorReporter, NoErrorReporter, SentryErrorReporter
from htco_platform.http import new_async_http_client
from htco_platform.jwt import HttpJwksSource, JwtVerifier
from htco_platform.logs import configure_logging
from htco_platform.settings import ConfigurationError, load_settings
from htco_platform.supabase_admin import (
    AlreadyRegisteredError,
    SupabaseInviter,
)
from htco_platform.supabase_admin import InvitationFailedError as SupabaseInvitationError
from htco_platform.tracing import new_tracer_provider

# Supabase access tokens for signed-in users carry this audience.
AUTH_AUDIENCE = "authenticated"

# sysexits.h EX_CONFIG: the process cannot start because its configuration is wrong.
EXIT_CONFIGURATION_ERROR = 78


def _serve(app: FastAPI, settings: ApiSettings) -> None:
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # logging is already configured as JSON
        access_log=False,  # the request-context middleware writes the access log
        server_header=False,  # do not advertise the server software (ASVS 13.4.6)
    )


def main(
    *,
    serve: Callable[[FastAPI, ApiSettings], None] = _serve,
    new_error_reporter: Callable[[ApiSettings], ErrorReporter] | None = None,
) -> int:
    """Load and validate settings, configure logging, then serve. Bad settings exit with 78.

    After the server stops, the error reporter is closed so queued reports are delivered.
    """
    try:
        settings = load_settings(ApiSettings)
    except ConfigurationError as error:
        sys.stderr.write(f"htco-api: invalid configuration\n{error}\n")
        return EXIT_CONFIGURATION_ERROR

    configure_logging(level=settings.log_level)
    reporter = (new_error_reporter or _error_reporter)(settings)
    tracer_provider = new_tracer_provider(
        service_name="htco-api", environment=settings.environment.value
    )
    wiring = _wiring(settings, tracer_provider)
    structlog.get_logger(__name__).info(
        "api.starting",
        environment=settings.environment.value,
        release=settings.release,
        host=settings.host,
        port=settings.port,
        error_monitoring="sentry" if isinstance(reporter, SentryErrorReporter) else "off",
        # Off locally without HTCO_SUPABASE_URL and HTCO_DATABASE_URL: authenticated routes 503.
        authentication="on" if wiring.authenticator is not None else "off",
        invitations="on" if wiring.inviter is not None else "off",
    )
    readiness_checks = (
        [DatabaseCheck(settings.database_url.get_secret_value())]
        if settings.database_url is not None
        else []
    )
    options = AppOptions(
        readiness_checks=readiness_checks,
        diagnostics=settings.environment is not Environment.PRODUCTION,
        error_reporter=reporter,
        tracer_provider=tracer_provider,
        authenticator=wiring.authenticator,
        shops=wiring.shops,
        access=wiring.access,
        inviter=wiring.inviter,
        resources=wiring.resources,
    )
    try:
        serve(create_app(options), settings)
    finally:
        reporter.close()
    return 0


class _HttpClient:
    """Closes the shared outgoing HTTP client when the app stops."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._client.aclose()


class _Inviter:
    """Supabase's inviter, speaking the application's and the API's error types."""

    def __init__(self, inviter: SupabaseInviter) -> None:
        self._inviter = inviter

    async def invite(self, email: str) -> uuid.UUID:
        try:
            return await self._inviter.invite(email)
        except AlreadyRegisteredError:
            raise InviteeAlreadyRegisteredError(email) from None
        except SupabaseInvitationError as error:
            raise InvitationFailedError(str(error)) from None


@dataclass(frozen=True)
class _Wiring:
    authenticator: Authenticator | None = None
    shops: ShopReader | None = None
    access: AccessStore | None = None
    inviter: _Inviter | None = None
    resources: list[Resource] = field(default_factory=list)


def _wiring(settings: ApiSettings, tracer_provider: TracerProvider) -> _Wiring:
    """Sign-in, memberships, shops and access management; needs the Supabase and database URLs."""
    if (
        settings.auth_issuer is None
        or settings.auth_jwks_url is None
        or settings.database_url is None
        or settings.supabase_url is None
    ):
        return _Wiring()
    database = Database(settings.database_url.get_secret_value())
    http = new_async_http_client(tracer_provider=tracer_provider)
    verifier = JwtVerifier(
        issuer=settings.auth_issuer,
        audience=AUTH_AUDIENCE,
        jwks=HttpJwksSource(http, settings.auth_jwks_url),
    )
    inviter = None
    if settings.supabase_secret_key is not None:
        inviter = _Inviter(
            SupabaseInviter(
                http,
                supabase_url=str(settings.supabase_url),
                secret_key=settings.supabase_secret_key.get_secret_value(),
                redirect_to=None
                if settings.invite_redirect_url is None
                else str(settings.invite_redirect_url),
            )
        )
    return _Wiring(
        authenticator=Authenticator(verifier, PostgresMembershipReader(database)),
        shops=PostgresShopReader(database),
        access=PostgresAccessStore(database),
        inviter=inviter,
        resources=[database, _HttpClient(http)],
    )


def _error_reporter(settings: ApiSettings) -> ErrorReporter:
    if settings.sentry_dsn is None:
        return NoErrorReporter()
    return SentryErrorReporter.from_dsn(
        settings.sentry_dsn.get_secret_value(),
        environment=settings.environment.value,
        release=settings.release,
    )
