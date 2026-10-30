"""Etsy webhook settings and wiring (slice 5.3): off by default, on only with a valid signing
secret, which never appears in an error."""

import pytest
from pydantic import ValidationError

from htco_api.main import etsy_webhook_ingress, main
from htco_api.settings import ApiSettings
from htco_application.etsy_webhooks import EtsyWebhookIngress, WindowLimiter
from htco_persistence.database import Database
from tests.api.test_main import _FakeServer
from tests.api.test_main import environment as environment  # noqa: PLC0414 - a fixture
from tests.api.test_main import restore_logging as restore_logging  # noqa: PLC0414
from tests.webhook_fakes import SECRET_TEXT


def _settings(**values: object) -> ApiSettings:
    return ApiSettings.model_validate({"environment": "local"} | values)


def test_webhooks__off_by_default() -> None:
    settings = _settings()
    assert settings.etsy_webhooks_enabled is False
    assert etsy_webhook_ingress(settings, Database("postgresql://x@db.invalid/x")) is None


def test_webhooks__enabled_without_the_secret__refused() -> None:
    with pytest.raises(ValidationError, match="HTCO_ETSY_WEBHOOK_SECRET"):
        _settings(etsy_webhooks_enabled=True)


def test_webhooks__an_invalid_secret__refused_without_repeating_it() -> None:
    with pytest.raises(ValidationError) as raised:
        _settings(etsy_webhook_secret="whsec_not-base64-zzz!!")  # noqa: S106 - fake
    assert "not-base64-zzz" not in str(raised.value)


def test_webhooks__a_secret_alone_keeps_them_off() -> None:
    settings = _settings(etsy_webhook_secret=SECRET_TEXT)
    assert etsy_webhook_ingress(settings, Database("postgresql://x@db.invalid/x")) is None


def test_webhooks__enabled_with_the_secret__the_ingress_is_wired() -> None:
    settings = _settings(etsy_webhooks_enabled=True, etsy_webhook_secret=SECRET_TEXT)
    assert isinstance(
        etsy_webhook_ingress(settings, Database("postgresql://x@db.invalid/x")), EtsyWebhookIngress
    )


@pytest.mark.usefixtures("restore_logging")
def test_main__webhooks_enabled__the_route_has_its_ingress_and_limiter(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("HTCO_ENVIRONMENT", "local")
    environment.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    environment.setenv("HTCO_SUPABASE_URL", "http://127.0.0.1:54321")
    environment.setenv("HTCO_ETSY_WEBHOOKS_ENABLED", "true")
    environment.setenv("HTCO_ETSY_WEBHOOK_SECRET", SECRET_TEXT)
    serve = _FakeServer()

    main(serve=serve)

    [(app, _)] = serve.calls
    assert isinstance(app.state.etsy_webhooks, EtsyWebhookIngress)
    assert isinstance(app.state.etsy_webhook_limiter, WindowLimiter)
    assert isinstance(app.state.etsy_webhook_ip_limiter, WindowLimiter)
    assert SECRET_TEXT not in repr(app.state.etsy_webhooks)
