"""Process entry point and composition root: `python -m htco_api`."""

import contextlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import structlog
import uvicorn
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from htco_api.access import InvitationFailedError
from htco_api.app import AppOptions, Resource, create_app
from htco_api.auth import Authenticator, VerifiedTokenReader
from htco_api.problems import AUTH_UNAVAILABLE, ProblemError
from htco_api.settings import ApiSettings, Environment
from htco_api.web_auth import WebAuth
from htco_application.access.management import (
    AccessStore,
    InvitedUser,
    InviteeAlreadyRegisteredError,
)
from htco_application.mfa import Enrollment, Factor
from htco_application.shops import ShopReader
from htco_application.web_sessions import (
    CredentialsRefusedError,
    IssuedTokens,
    TooManyAttemptsError,
    WeakPasswordError,
)
from htco_persistence.access import PostgresAccessStore
from htco_persistence.database import Database
from htco_persistence.identity import PostgresMembershipReader
from htco_persistence.readiness import DatabaseCheck
from htco_persistence.shops import PostgresShopReader
from htco_persistence.web_sessions import PostgresSessionStore
from htco_platform.crypto import Sealer
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
from htco_platform.supabase_auth import (
    AuthServiceError,
    AuthTokens,
    InvalidCredentialsError,
    RateLimitedError,
    SupabaseAuth,
)
from htco_platform.supabase_auth import WeakPasswordError as SupabaseWeakPasswordError
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
        web_sign_in="on" if wiring.web_auth is not None else "off",
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
        web_auth=wiring.web_auth,
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

    async def invite(self, email: str) -> InvitedUser:
        try:
            invited = await self._inviter.invite(email)
        except AlreadyRegisteredError:
            raise InviteeAlreadyRegisteredError(email) from None
        except SupabaseInvitationError as error:
            raise InvitationFailedError(str(error)) from None
        return InvitedUser(invited.user_id, invited.link)


class _AuthProvider:
    """Supabase Auth, speaking the application's and the API's error types."""

    def __init__(self, auth: SupabaseAuth) -> None:
        self._auth = auth

    async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens:
        return await self._call(self._auth.sign_in_with_password(email, password))

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        return await self._call(self._auth.refresh(refresh_token))

    async def sign_out(self, access_token: str) -> None:
        with contextlib.suppress(AuthServiceError):  # best effort: our session is already gone
            await self._auth.sign_out(access_token)

    async def set_password(self, access_token: str, password: str) -> None:
        try:
            await self._auth.set_password(access_token, password)
        except SupabaseWeakPasswordError:
            raise WeakPasswordError("the password is too weak") from None
        except InvalidCredentialsError:
            raise CredentialsRefusedError from None
        except RateLimitedError:
            raise TooManyAttemptsError("the sign-in service is rate limiting") from None
        except AuthServiceError:
            raise ProblemError(AUTH_UNAVAILABLE) from None

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment:
        try:
            enrolled = await self._auth.enroll_totp(access_token, friendly_name=friendly_name)
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return Enrollment(enrolled.factor_id, enrolled.qr_code, enrolled.secret, enrolled.uri)

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens:
        return await self._call(self._auth.verify_totp(access_token, factor_id, code))

    async def factors_of(self, access_token: str) -> list[Factor]:
        try:
            found = await self._auth.factors(access_token)
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return [
            Factor(id=f.id, verified=f.verified, name=f.friendly_name)
            for f in found
            if f.factor_type == "totp"
        ]

    async def remove_factor(self, access_token: str, factor_id: str) -> None:
        try:
            await self._auth.unenroll(access_token, factor_id)
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None

    @staticmethod
    async def _call(pending: Awaitable[AuthTokens]) -> IssuedTokens:
        try:
            tokens = await pending
        except InvalidCredentialsError:
            raise CredentialsRefusedError from None
        except RateLimitedError:
            raise TooManyAttemptsError("the sign-in service is rate limiting") from None
        except AuthServiceError:
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return IssuedTokens(tokens.access_token, tokens.refresh_token, tokens.expires_at)


@dataclass(frozen=True)
class _Wiring:
    authenticator: Authenticator | None = None
    shops: ShopReader | None = None
    access: AccessStore | None = None
    inviter: _Inviter | None = None
    web_auth: WebAuth | None = None
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
    web_auth = None
    if settings.session_key is not None and settings.supabase_publishable_key is not None:
        provider = _AuthProvider(
            SupabaseAuth(
                http,
                supabase_url=str(settings.supabase_url),
                publishable_key=settings.supabase_publishable_key,
            )
        )
        web_auth = WebAuth(
            auth=provider,
            mfa=provider,
            tokens=VerifiedTokenReader(verifier),
            store=PostgresSessionStore(
                database, Sealer.from_base64(settings.session_key.get_secret_value())
            ),
            allowed_origins=settings.web_origin_list,
        )
    return _Wiring(
        web_auth=web_auth,
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
