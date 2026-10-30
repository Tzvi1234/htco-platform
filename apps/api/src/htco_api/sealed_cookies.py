"""Short-lived sealed cookies for one hand-off between two requests (Codex security review, owner
gate v4).

Some values must ride with the browser from a redirect to the page's next request, but the page
must never read them: an invitation or password reset link's Supabase tokens, and the code and
state Etsy sends back after consent. The API keeps them in an `__Host-` cookie (`Secure`,
`HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`), sealed with AES-256-GCM and bound to the
cookie's name, with their own expiry inside the seal: a cookie kept past its `Max-Age`, copied
into another cookie or edited does not open.
"""

import base64
import binascii
import json
from datetime import datetime, timedelta

from fastapi import Request, Response

from htco_platform.crypto import Sealer, SealingError

LINK_COOKIE = "__Host-htco_link"
ETSY_CALLBACK_COOKIE = "__Host-htco_etsy_callback"
# Browsers drop a cookie past about 4 KB; a larger value is refused before it is set.
_MAX_COOKIE_BYTES = 3800


class CookieTooLargeError(Exception):
    """The sealed value does not fit in a cookie."""


def set_sealed(  # noqa: PLR0913 - the response, the key, the cookie and its lifetime
    response: Response,
    sealer: Sealer,
    name: str,
    payload: dict[str, str],
    *,
    now: datetime,
    lifetime: timedelta,
) -> None:
    expires = int((now + lifetime).timestamp())
    plaintext = json.dumps({"v": payload, "exp": expires}, separators=(",", ":"))
    sealed = sealer.seal(plaintext, context=_context(name))
    value = base64.urlsafe_b64encode(sealed).rstrip(b"=").decode("ascii")
    if len(name) + len(value) > _MAX_COOKIE_BYTES:
        raise CookieTooLargeError(name)
    response.set_cookie(
        name,
        value,
        max_age=int(lifetime.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def open_sealed(
    request: Request, sealer: Sealer, name: str, *, now: datetime
) -> dict[str, str] | None:
    """The cookie's payload; None when it is missing, expired, tampered or not ours."""
    value = request.cookies.get(name, "")
    if not value:
        return None
    try:
        blob = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        opened = json.loads(sealer.open(blob, context=_context(name)))
    except binascii.Error, ValueError, SealingError:
        return None
    payload = opened.get("v") if isinstance(opened, dict) else None
    expires = opened.get("exp") if isinstance(opened, dict) else None
    if not isinstance(payload, dict) or not isinstance(expires, int):
        return None
    if now.timestamp() >= expires:
        return None
    return {k: v for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}


def expire(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")


def expired_header(name: str) -> str:
    """The `Set-Cookie` value that removes the cookie, for answers built without a Response."""
    return f"{name}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=strict"


def _context(name: str) -> bytes:
    return b"htco-sealed-cookie:" + name.encode("ascii")
