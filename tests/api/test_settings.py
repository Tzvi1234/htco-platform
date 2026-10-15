"""Sign-in settings derived from the Supabase project URL (slice 2.3)."""

import base64

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
            "web_origins": "https://dash.ht-co.app, http://localhost:5173",
        }
    )
    assert settings.web_origin_list == ("https://dash.ht-co.app", "http://localhost:5173")
    assert settings.session_key is not None


@pytest.mark.parametrize("key", ["not-base64!", "c2hvcnQ="])
def test_session_key__must_be_a_32_byte_base64_key(key: str) -> None:
    with pytest.raises(ValidationError, match="HTCO_SESSION_KEY"):
        ApiSettings.model_validate({"environment": "local", "session_key": key})


def test_etsy_settings__are_optional__with_a_default_quota_floor() -> None:
    settings = ApiSettings.model_validate({"environment": "production", **_DEPLOYED})

    assert settings.etsy_keystring is None
    assert settings.etsy_shared_secret is None
    assert settings.etsy_daily_quota_floor == 500


def test_etsy_settings__secrets__never_appear_in_repr() -> None:
    settings = ApiSettings.model_validate(
        {
            "environment": "local",
            "etsy_keystring": "fakekeystring",
            "etsy_shared_secret": "fake-shared-secret",
        }
    )

    assert settings.etsy_keystring is not None
    assert settings.etsy_shared_secret is not None
    assert settings.etsy_keystring.get_secret_value() == "fakekeystring"
    assert "fakekeystring" not in repr(settings)
    assert "fake-shared-secret" not in repr(settings)
    assert "fake-shared-secret" not in str(settings.model_dump())


@pytest.mark.parametrize("blank", ["", "  "])
def test_etsy_settings__blank__are_unset(blank: str) -> None:
    settings = ApiSettings.model_validate(
        {"environment": "local", "etsy_keystring": blank, "etsy_shared_secret": blank}
    )

    assert settings.etsy_keystring is None
    assert settings.etsy_shared_secret is None


@pytest.mark.parametrize(
    "given", [{"etsy_keystring": "fakekeystring"}, {"etsy_shared_secret": "fake-shared-secret"}]
)
def test_etsy_settings__one_without_the_other__is_refused(given: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="HTCO_ETSY_KEYSTRING and HTCO_ETSY_SHARED_SECRET"):
        ApiSettings.model_validate({"environment": "local", **given})


def test_etsy_settings__negative_quota_floor__is_refused() -> None:
    with pytest.raises(ValidationError, match="etsy_daily_quota_floor"):
        ApiSettings.model_validate({"environment": "local", "etsy_daily_quota_floor": -1})


_DEPLOYED = {"database_url": "postgresql://x@db.invalid/x", "supabase_url": "https://a.b"}

# The Etsy token vault's keys (slice 4.2) ---------------------------------------------------------

VAULT_KEY_A = "key-a:" + base64.b64encode(b"a" * 32).decode()
VAULT_KEY_B = "key-b:" + base64.b64encode(b"b" * 32).decode()


def test_token_vault_keys__current_and_previous__build_the_key_ring() -> None:
    settings = ApiSettings.model_validate(
        {
            "environment": "local",
            "token_vault_key": VAULT_KEY_B,
            "token_vault_previous_keys": VAULT_KEY_A,
        }
    )

    ring = settings.token_key_ring
    assert ring is not None
    assert ring.key_ids == ("key-b", "key-a")


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_token_vault_key__blank_or_unset__leaves_the_vault_off(blank: str | None) -> None:
    settings = ApiSettings.model_validate(
        {"environment": "production", "database_url": "postgresql://x@db.invalid/x"}
        | {"supabase_url": "https://abc.supabase.co"}
        | ({} if blank is None else {"token_vault_key": blank, "token_vault_previous_keys": blank})
    )
    assert settings.token_key_ring is None


@pytest.mark.ac("AC-4.2.3")
@pytest.mark.parametrize(
    ("current", "previous"),
    [
        ("key-a:not-base64!", None),
        ("key-a:c2hvcnQ=", None),
        (VAULT_KEY_B, "key-b:" + base64.b64encode(b"c" * 32).decode()),
        (None, VAULT_KEY_A),
    ],
    ids=["not-base64", "short", "duplicate-id", "previous-without-current"],
)
def test_token_vault_keys__invalid__name_the_setting_but_never_the_value(
    current: str | None, previous: str | None
) -> None:
    with pytest.raises(ValidationError, match="HTCO_TOKEN_VAULT") as raised:
        ApiSettings.model_validate(
            {"environment": "local"}
            | ({} if current is None else {"token_vault_key": current})
            | ({} if previous is None else {"token_vault_previous_keys": previous})
        )
    message = str(raised.value)
    for value in (current, previous):
        if value is not None:
            assert value.partition(":")[2] not in message


@pytest.mark.ac("AC-4.2.3")
def test_token_vault_keys__hidden_in_the_settings_repr() -> None:
    settings = ApiSettings.model_validate({"environment": "local", "token_vault_key": VAULT_KEY_A})
    assert VAULT_KEY_A.partition(":")[2] not in repr(settings)
    assert VAULT_KEY_A.partition(":")[2] not in str(settings.model_dump())
