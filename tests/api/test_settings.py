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


def test_web_sign_in_settings__origins_and_keys() -> None:
    import base64  # noqa: PLC0415 - local to this test

    key = base64.b64encode(b"k" * 32).decode()
    settings = ApiSettings.model_validate(
        {
            "environment": "local",
            "session_key": key,
            "supabase_publishable_key": "sb_publishable_x",
            "web_origins": "https://app.ht-co.app, http://localhost:5173",
        }
    )
    assert settings.web_origin_list == ("https://app.ht-co.app", "http://localhost:5173")
    assert settings.session_key is not None


@pytest.mark.parametrize("key", ["not-base64!", "c2hvcnQ="])
def test_session_key__must_be_a_32_byte_base64_key(key: str) -> None:
    with pytest.raises(ValidationError, match="HTCO_SESSION_KEY"):
        ApiSettings.model_validate({"environment": "local", "session_key": key})
