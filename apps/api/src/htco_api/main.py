"""Process entry point and composition root: `python -m htco_api`."""

import contextlib
import random
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import structlog
import uvicorn
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from htco_api.access import InvitationFailedError
from htco_api.app import AppOptions, Resource, create_app
from htco_api.auth import Authenticator, VerifiedTokenReader
from htco_api.automations import AutomationServices
from htco_api.browser_environments import BrowserEnvironmentServices
from htco_api.incidents import IncidentCenter
from htco_api.problems import AUTH_UNAVAILABLE, ProblemError
from htco_api.settings import ApiSettings, Environment
from htco_api.web_auth import WebAuth
from htco_application.access.management import (
    AccessStore,
    InvitedUser,
    InviteeAlreadyRegisteredError,
)
from htco_application.automation.deployed import WORKER_AUTOMATIONS
from htco_application.automation.durable import (
    DurableAutomationEngine,
    DurableExecutionStore,
    HandlerContext,
    HandlerOutcome,
)
from htco_application.automation.records import registry_from_rows
from htco_application.etsy_connect import EtsyConnect
from htco_application.etsy_tokens import VaultAccessTokenProvider
from htco_application.etsy_webhooks import EtsyWebhookIngress, WebhookSecret, WindowLimiter
from htco_application.execution.gateway import UnavailableGateway
from htco_application.incidents import IncidentService
from htco_application.jobs import JobError
from htco_application.marketplace import MarketplacePort, QuotaMonitor
from htco_application.mfa import Enrollment, Factor
from htco_application.notifications import SecurityNotices
from htco_application.order_history import OrderSync, adapter_pages
from htco_application.order_intake import QueuedSyncRequests
from htco_application.passkeys import (
    PASSKEY,
    TOTP,
    Ceremony,
    FactorAuditLog,
    PasskeyChallenge,
    PasskeysUnavailableError,
)
from htco_application.shop_capabilities import CapabilityChecks
from htco_application.shop_dashboard import ShopDashboard
from htco_application.shop_data_cache import ShopDataCache
from htco_application.shop_integrations import IntegrationReader, ShopRemover, TokenVault
from htco_application.shops import ShopReader
from htco_application.web_sessions import (
    CredentialsRefusedError,
    IssuedTokens,
    MfaCodeRequiredError,
    ResetEmailNotSentError,
    SamePasswordError,
    TooManyAttemptsError,
    WeakPasswordError,
)
from htco_application.workforce.registry import Registry
from htco_integrations.etsy import EtsyAdapter, EtsyConfig, EtsyCredentials, app_key_fingerprint
from htco_integrations.etsy_oauth import EtsyOAuthClient
from htco_persistence.access import PostgresAccessStore
from htco_persistence.audit import PostgresAuditLog
from htco_persistence.automation import (
    PostgresAutomationCatalog,
    PostgresExecutionReader,
    PostgresExecutionStore,
)
from htco_persistence.browser_environments import (
    PostgresEnvironmentReader,
    PostgresEnvironmentStore,
)
from htco_persistence.database import Database
from htco_persistence.etsy_connect import PostgresPendingAuthorizations, PostgresShopConnector
from htco_persistence.etsy_orders import PostgresOrderStore
from htco_persistence.etsy_webhooks import PostgresWebhookInbox, PostgresWebhookShops
from htco_persistence.fulfillment import PostgresIncidentReader, PostgresIncidentStore
from htco_persistence.idempotency import PostgresIdempotencyStore
from htco_persistence.identity import PostgresMembershipReader
from htco_persistence.jobs import PostgresJobQueue
from htco_persistence.notifications import (
    PostgresNotificationInbox,
    PostgresSecurityNoticeStore,
)
from htco_persistence.operations_health import PostgresOperationsHealth
from htco_persistence.readiness import DatabaseCheck
from htco_persistence.relay import PostgresRelayStore
from htco_persistence.shop_integrations import (
    PostgresCapabilityStore,
    PostgresIntegrationReader,
    PostgresRefreshLock,
    PostgresTokenVault,
)
from htco_persistence.shops import PostgresShopReader, PostgresShopRemover
from htco_persistence.status import PostgresChainReader
from htco_persistence.web_sessions import PostgresSessionStore
from htco_platform.crypto import EnvelopeSealer, Sealer
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
    MfaRequiredError,
    RateLimitedError,
    SupabaseAuth,
    WebAuthnDisabledError,
)
from htco_platform.supabase_auth import SamePasswordError as SupabaseSamePasswordError
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
        # Off without HTCO_TOKEN_VAULT_KEY: no Etsy token can be stored or read (slice 4.2).
        etsy_token_vault="on" if wiring.token_vault is not None else "off",
        # Off without the Etsy credentials, the redirect URI, the vault key or web sign-in.
        etsy_connect="on" if wiring.etsy is not None else "off",
        # Off without HTCO_AUTOMATIONS_ENABLED: automations are listed, never started (5.10).
        # Off without HTCO_ETSY_WEBHOOKS_ENABLED and HTCO_ETSY_WEBHOOK_SECRET (slice 5.3).
        etsy_webhooks="on" if wiring.etsy_webhooks is not None else "off",
        automation_triggers="on"
        if wiring.automations is not None and wiring.automations.engine is not None
        else "off",
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
        etsy=wiring.etsy,
        integrations=wiring.integrations,
        etsy_quota=wiring.etsy_quota,
        capability_checks=wiring.capability_checks,
        token_vault=wiring.token_vault,
        shop_remover=wiring.shop_remover,
        dashboard=wiring.dashboard,
        automations=wiring.automations,
        etsy_webhooks=wiring.etsy_webhooks,
        etsy_webhook_limiter=None
        if wiring.etsy_webhooks is None
        else WindowLimiter(settings.etsy_webhook_rate_per_second),
        browser_environments=wiring.browser_environments,
        execution_chain=wiring.execution_chain,
        incident_center=wiring.incident_center,
        security_notices=wiring.security_notices,
        operations=wiring.operations,
        resources=wiring.resources,
    )
    try:
        serve(create_app(options), settings)
    finally:
        reporter.close()
    return 0


class _AppKeyCheck:
    """At startup, marks connections whose tokens belong to another Etsy app key `needs_reauth`
    (after the key was replaced). A failure is logged, never stops the app: the vault treats such
    rows as `needs_reauth` whenever it reads them anyway."""

    def __init__(self, vault: PostgresTokenVault) -> None:
        self._vault = vault

    async def start(self) -> None:
        try:
            await self._vault.retire_other_app_keys()
        except Exception as error:
            structlog.get_logger(__name__).warning(
                "api.app_key_check_failed", error=type(error).__name__
            )

    async def stop(self) -> None:
        return None


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
        except SupabaseSamePasswordError:
            raise SamePasswordError("the password is unchanged") from None
        except InvalidCredentialsError:
            raise CredentialsRefusedError from None
        except RateLimitedError:
            raise TooManyAttemptsError("the sign-in service is rate limiting") from None
        except MfaRequiredError:
            raise MfaCodeRequiredError("an authenticator code is required") from None
        except AuthServiceError:
            raise ProblemError(AUTH_UNAVAILABLE) from None

    async def send_password_reset(self, email: str, redirect_to: str) -> None:
        try:
            await self._auth.send_password_reset(email, redirect_to)
        except (AuthServiceError, RateLimitedError) as error:
            raise ResetEmailNotSentError(type(error).__name__) from None

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
            Factor(id=f.id, verified=f.verified, name=f.friendly_name, kind=f.factor_type)
            for f in found
            if f.factor_type in {TOTP, PASSKEY}
        ]

    async def remove_factor(self, access_token: str, factor_id: str) -> None:
        try:
            await self._auth.unenroll(access_token, factor_id)
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None

    # Passkeys (issue #113): Supabase Auth's WebAuthn MFA factor.

    async def enroll_passkey(self, access_token: str, *, friendly_name: str) -> str:
        try:
            return await self._auth.enroll_webauthn(access_token, friendly_name=friendly_name)
        except WebAuthnDisabledError:
            raise PasskeysUnavailableError("WebAuthn MFA is off in Supabase Auth") from None
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None

    async def challenge_passkey(self, access_token: str, factor_id: str) -> PasskeyChallenge:
        try:
            found = await self._auth.challenge_webauthn(access_token, factor_id)
        except WebAuthnDisabledError:
            raise PasskeysUnavailableError("WebAuthn MFA is off in Supabase Auth") from None
        except AuthServiceError, InvalidCredentialsError, RateLimitedError:
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return PasskeyChallenge(
            factor_id, found.challenge_id, Ceremony(found.ceremony), found.options
        )

    async def verify_passkey(
        self,
        access_token: str,
        factor_id: str,
        challenge_id: str,
        ceremony: Ceremony,
        credential: Mapping[str, Any],
    ) -> IssuedTokens:
        try:
            tokens = await self._auth.verify_webauthn(
                access_token, factor_id, challenge_id, ceremony.value, dict(credential)
            )
        except WebAuthnDisabledError:
            raise PasskeysUnavailableError("WebAuthn MFA is off in Supabase Auth") from None
        except InvalidCredentialsError:
            raise CredentialsRefusedError from None
        except RateLimitedError:
            raise TooManyAttemptsError("the sign-in service is rate limiting") from None
        except AuthServiceError:
            raise ProblemError(AUTH_UNAVAILABLE) from None
        return IssuedTokens(tokens.access_token, tokens.refresh_token, tokens.expires_at)

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
    token_vault: TokenVault | None = None
    shop_remover: ShopRemover | None = None
    # The connect flow (slice 4.3) and the shop card's connection health (slice 4.4).
    etsy: EtsyConnect | None = None
    integrations: IntegrationReader | None = None
    # Etsy reads with refreshed tokens; no route calls it yet (Phase 5 ingests orders with it).
    marketplace: MarketplacePort | None = None
    # The same adapter's daily request budget and floor, for the shop card (AC-4.1.2).
    etsy_quota: QuotaMonitor | None = None
    # Read-only capability probes (slice 4.5), with the adapter; off when the connect flow is off.
    capability_checks: CapabilityChecks | None = None
    # The connected shop's live data for the dashboard: read-only adapter calls through a short
    # in-memory cache; off when the connect flow is off.
    dashboard: ShopDashboard | None = None
    automations: AutomationServices | None = None
    # Etsy webhooks (slice 5.3): on with HTCO_ETSY_WEBHOOKS_ENABLED and the signing secret.
    etsy_webhooks: EtsyWebhookIngress | None = None
    browser_environments: BrowserEnvironmentServices | None = None
    execution_chain: PostgresChainReader | None = None
    incident_center: IncidentCenter | None = None
    security_notices: SecurityNotices | None = None
    operations: PostgresOperationsHealth | None = None
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
            password_reset_redirect=None
            if settings.password_reset_redirect_url is None
            else str(settings.password_reset_redirect_url),
            # Off unless the owner switches passkeys on (issue #113): the add-on and the RP config.
            passkeys=provider if settings.passkeys_enabled else None,
            factor_audit=FactorAuditLog(
                organizations_of=PostgresSecurityNoticeStore(database).organizations_of,
                log=PostgresAuditLog(database),
                offload=anyio.to_thread.run_sync,
            ),
        )
    key_ring = settings.token_key_ring
    app_key = (
        None
        if settings.etsy_keystring is None
        else app_key_fingerprint(settings.etsy_keystring.get_secret_value())
    )
    token_vault = (
        None
        if key_ring is None
        else PostgresTokenVault(database, EnvelopeSealer(key_ring), app_key_fingerprint=app_key)
    )
    etsy, marketplace, checks, dashboard = _etsy_wiring(
        settings, database, http, token_vault, web_auth
    )
    resources: list[Resource] = [database, _HttpClient(http)]
    if token_vault is not None and app_key is not None:
        resources.append(_AppKeyCheck(token_vault))
    if dashboard is not None and dashboard.history is not None:
        resources.append(dashboard.history)  # running syncs stop with the app
    return _Wiring(
        web_auth=web_auth,
        token_vault=token_vault,
        etsy=etsy,
        marketplace=marketplace,
        etsy_quota=marketplace,
        capability_checks=checks,
        dashboard=dashboard,
        integrations=PostgresIntegrationReader(database, app_key_fingerprint=app_key),
        shop_remover=PostgresShopRemover(database),
        authenticator=Authenticator(verifier, PostgresMembershipReader(database)),
        shops=PostgresShopReader(database),
        access=PostgresAccessStore(database),
        inviter=inviter,
        automations=automation_services(database, enabled=settings.automations_enabled),
        etsy_webhooks=etsy_webhook_ingress(settings, database),
        browser_environments=BrowserEnvironmentServices(
            PostgresEnvironmentReader(database), PostgresEnvironmentStore(database)
        ),
        execution_chain=PostgresChainReader(database),
        incident_center=IncidentCenter(
            reader=PostgresIncidentReader(database),
            service=IncidentService(
                PostgresIncidentStore(database),
                clock=lambda: datetime.now(UTC),
                offload=anyio.to_thread.run_sync,
            ),
            inbox=PostgresNotificationInbox(database),
            relay=PostgresRelayStore(database),
        ),
        security_notices=SecurityNotices(
            PostgresSecurityNoticeStore(database),
            clock=lambda: datetime.now(UTC),
            offload=anyio.to_thread.run_sync,
        ),
        operations=PostgresOperationsHealth(database),
        resources=resources,
    )


def etsy_webhook_ingress(settings: ApiSettings, database: Database) -> EtsyWebhookIngress | None:
    """The webhook ingress when enabled with its secret (validated with the settings)."""
    secret = settings.etsy_webhook_secret
    if not settings.etsy_webhooks_enabled or secret is None:
        return None
    return EtsyWebhookIngress(
        secret=WebhookSecret.parse(secret.get_secret_value()),
        inbox=PostgresWebhookInbox(database),
        shops=PostgresWebhookShops(database),
        clock=lambda: datetime.now(UTC),
        offload=anyio.to_thread.run_sync,
    )


def _etsy_wiring(
    settings: ApiSettings,
    database: Database,
    http: httpx.AsyncClient,
    vault: PostgresTokenVault | None,
    web_auth: WebAuth | None,
) -> tuple[EtsyConnect | None, EtsyAdapter | None, CapabilityChecks | None, ShopDashboard | None]:
    """The connect flow, the adapter, the capability probes and the dashboard's live shop data,
    when every piece they need is configured. The adapter only reads: it refuses any other method
    towards Etsy's shop API."""
    if (
        vault is None
        or web_auth is None
        or settings.session_key is None
        or settings.etsy_keystring is None
        or settings.etsy_shared_secret is None
        or settings.etsy_redirect_uri is None
    ):
        return None, None, None, None
    credentials = EtsyCredentials(
        keystring=settings.etsy_keystring.get_secret_value(),
        shared_secret=settings.etsy_shared_secret.get_secret_value(),
    )
    oauth = EtsyOAuthClient(http, credentials=credentials)
    tokens = VaultAccessTokenProvider(
        vault, oauth, PostgresRefreshLock(database), clock=lambda: datetime.now(UTC)
    )
    adapter = EtsyAdapter(
        http,
        credentials=credentials,
        access_tokens=tokens,
        config=EtsyConfig(daily_quota_floor=settings.etsy_daily_quota_floor),
    )
    etsy = EtsyConnect(
        authorization_server=oauth,
        directory=adapter,
        pending=PostgresPendingAuthorizations(
            database, Sealer.from_base64(settings.session_key.get_secret_value())
        ),
        connector=PostgresShopConnector(database, vault),
        audit=PostgresAuditLog(database),
        redirect_uri=settings.etsy_redirect_uri,
    )
    checks = CapabilityChecks(
        vault=vault,
        marketplace=adapter,
        store=PostgresCapabilityStore(database),
        tokens=tokens,
        # Etsy webhooks (slice 5.3): configured when enabled; Etsy's deliveries prove them.
        webhooks_configured=settings.etsy_webhooks_enabled,
    )
    # The stored order history (slice 5.4): synced by the worker's Etsy Order Intake
    # (HTCO_ORDER_SYNC_IN_API off, the default); on, this process runs it as the rollback.
    history = OrderSync(
        store=PostgresOrderStore(database),
        pages=adapter_pages(adapter, tokens),
        clock=lambda: datetime.now(UTC),
        audit=PostgresAuditLog(database),
        requests=None
        if settings.order_sync_in_api
        else QueuedSyncRequests(PostgresJobQueue(database), offload=anyio.to_thread.run_sync),
    )
    dashboard = ShopDashboard(
        connections=vault,
        source=adapter,
        tokens=tokens,
        cache=ShopDataCache(clock=lambda: datetime.now(UTC)),
        successes=vault,
        history=history,
        # The Orders screen's bulk actions (slice 5.7): the export's audit, and "Re-sync
        # selected" on the queue (the worker's Etsy Order Intake re-reads, GET-only), at most
        # five requests a minute per shop and instance.
        audit=PostgresAuditLog(database),
        resync=QueuedSyncRequests(PostgresJobQueue(database), offload=anyio.to_thread.run_sync),
        resync_limiter=WindowLimiter(5, window=timedelta(minutes=1)),
    )
    return etsy, adapter, checks, dashboard


async def _runs_on_the_worker(ctx: HandlerContext) -> HandlerOutcome:
    """The API's placeholder for a worker-deployed handler: the API creates executions and
    jobs, and the worker runs them; one never runs here."""
    raise JobError(f"{ctx.automation_id} runs on the worker, never in the API")


def api_engine(
    registry: Callable[[], Registry], store: DurableExecutionStore
) -> DurableAutomationEngine:
    """The API's create-only engine: the automations the worker deploys (`WORKER_AUTOMATIONS`)
    pass its "deployed" check, so a user's trigger of a ready one (the counters rebuild, 5.11
    step 2b) or an audited admin retry becomes a job for the worker; a draft or undeployed one
    is still refused before anything is written."""
    return DurableAutomationEngine(
        registry,
        store,
        dict.fromkeys(WORKER_AUTOMATIONS, _runs_on_the_worker),
        clock=lambda: datetime.now(UTC),
        jitter=random.random,
        gateway=UnavailableGateway(),
    )


def automation_services(database: Database, *, enabled: bool) -> AutomationServices:
    """The catalog and executions from the database; the engine only when enabled."""
    catalog = PostgresAutomationCatalog(database)
    store = PostgresExecutionStore(database)

    def registry() -> Registry:
        return registry_from_rows(catalog.current())

    engine = api_engine(registry, store) if enabled else None
    return AutomationServices(
        definitions=lambda: list(registry().automations.values()),
        executions=PostgresExecutionReader(database),
        shops=store,
        idempotency=PostgresIdempotencyStore(database),
        engine=engine,
    )


def _error_reporter(settings: ApiSettings) -> ErrorReporter:
    if settings.sentry_dsn is None:
        return NoErrorReporter()
    return SentryErrorReporter.from_dsn(
        settings.sentry_dsn.get_secret_value(),
        environment=settings.environment.value,
        release=settings.release,
    )
