"""The local WebAuthn relying party (issue #113): every origin is the RP ID or under it.

A browser refuses a passkey whose RP ID is not the origin's host or one of its parents, so an
origin like 127.0.0.1 next to `rp_id = "localhost"` can never work (local Codex on #171)."""

import tomllib
from pathlib import Path
from urllib.parse import urlsplit

CONFIG = Path(__file__).resolve().parents[2] / "supabase" / "config.toml"


def test_every_rp_origin_is_the_rp_id_or_under_it() -> None:
    webauthn = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["auth"]["webauthn"]
    rp_id = webauthn["rp_id"]

    hosts = [urlsplit(origin).hostname for origin in webauthn["rp_origins"]]

    assert hosts
    assert all(h == rp_id or (h or "").endswith(f".{rp_id}") for h in hosts), hosts
