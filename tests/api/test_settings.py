"""Sign-in settings derived from the Supabase project URL (slice 2.3)."""

import pytest
from pydantic import ValidationError

from htco_api.settings import ApiSettings


@pytest.mark.parametrize("url", ["https://abc.supabase.co", "https://abc.supabase.co/"])
def test_auth_urls__derive_from_the_supabase_url(url: str) -> None:
    settings = ApiSettings.model_validate({"environment": "local", "supabase_url": url})

    assert settings.auth_issuer == "https://abc.supabase.co/auth/v1"
    assert settings.auth_jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"


def test_auth_urls__without_supabase__are_none() -> None:
    settings = ApiSettings.model_validate({"environment": "local"})
    assert settings.auth_issuer is None
    assert settings.auth_jwks_url is None


def test_supabase_url__plain_http_is_refused_when_deployed() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        ApiSettings.model_validate(
            {
                "environment": "production",
                "database_url": "postgresql://x@db.invalid/x",
                "supabase_url": "http://abc.supabase.co",
            }
        )


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_optional_settings__are_unset(blank: str) -> None:
    settings = ApiSettings.model_validate(
        {
            "environment": "local",
            "database_url": blank,
            "supabase_url": blank,
            "supabase_secret_key": blank,
            "invite_redirect_url": blank,
        }
    )
    assert settings.database_url is None
    assert settings.supabase_url is None
    assert settings.supabase_secret_key is None
    assert settings.invite_redirect_url is None


def test_blank_database_url__is_refused_when_deployed() -> None:
    with pytest.raises(ValidationError, match="HTCO_DATABASE_URL"):
        ApiSettings.model_validate(
            {"environment": "production", "database_url": " ", "supabase_url": "https://a.b"}
        )
