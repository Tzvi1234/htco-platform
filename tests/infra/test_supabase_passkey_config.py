"""The local Supabase stack's passkey config (issue #113): WebAuthn MFA on for the local RP only.

A passkey is bound to its relying party, so the local stack (RP ID `localhost`) never shares one
with production (RP ID `ht-co.app`, set on the hosted project by the owner). Passkeys as the
first sign-in step stay off: they are a second factor here."""

import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CONFIG: dict[str, Any] = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "supabase" / "config.toml").read_text(encoding="utf-8")
)
AUTH: dict[str, Any] = CONFIG["auth"]


def test_webauthn_mfa__enroll_and_verify_on_locally() -> None:
    assert AUTH["mfa"]["web_authn"] == {"enroll_enabled": True, "verify_enabled": True}
    assert AUTH["mfa"]["totp"]["enroll_enabled"] is True  # the app stays available


def test_relying_party__localhost_only_with_local_origins() -> None:
    rp = AUTH["webauthn"]
    assert rp["rp_id"] == "localhost"
    assert rp["rp_display_name"] == "HTCO"
    hosts = {urlsplit(origin).hostname for origin in rp["rp_origins"]}
    assert hosts <= {"localhost", "127.0.0.1"}
    assert all(urlsplit(o).scheme == "http" for o in rp["rp_origins"])


def test_passkeys_are_not_a_first_sign_in_step() -> None:
    assert AUTH.get("passkey", {}).get("enabled", False) is False
