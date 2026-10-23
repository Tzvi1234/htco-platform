"""Email adapters for the `EmailPort` (slice 6.4, FR-EML-01).

- `LogEmailAdapter`: the default. It sends nothing and logs the kind and the idempotency key
  only (never the address, the subject or the body). Used until the owner sets up Resend's DNS
  records (SPF, DKIM, DMARC) for `office.ht-co.app`.
- `ResendEmailAdapter`: `POST https://api.resend.com/emails` with the outbox key as Resend's
  `Idempotency-Key`, so a delivery repeated after a crash sends one email. 429 and 5xx are
  temporary (the outbox retries with backoff); other 4xx are permanent. Built and tested against
  a mock transport only; OFF until `HTCO_EMAIL_PROVIDER=resend`.
"""

import logging
from typing import Any, override

import httpx

from htco_application.notifications import (
    EmailMessage,
    EmailPermanentError,
    EmailPort,
    EmailReceipt,
    EmailTemporaryError,
)

RESEND_URL = "https://api.resend.com/emails"
# 409: a concurrent request with the same idempotency key; 429: rate limited.
_RETRY = frozenset({409, 429})
_SERVER = 500

_log = logging.getLogger(__name__)


class LogEmailAdapter(EmailPort):
    def __init__(self) -> None:
        self.sent: list[str] = []  # idempotency keys, for tests and the admin's dry run

    @override
    async def send(self, message: EmailMessage) -> EmailReceipt:
        self.sent.append(message.idempotency_key)
        _log.info("email.logged: kind=%s key=%s", message.tags.get("kind"), message.idempotency_key)
        return EmailReceipt(provider_id=f"log:{len(self.sent)}")


class ResendEmailAdapter(EmailPort):
    def __init__(self, http: httpx.AsyncClient, *, api_key: str, timeout: float = 10.0) -> None:
        if not api_key.startswith("re_"):
            raise ValueError("a Resend API key starts with re_")
        self._http = http
        self._api_key = api_key
        self._timeout = timeout

    @override
    def __repr__(self) -> str:
        return "ResendEmailAdapter(<key hidden>)"

    @override
    async def send(self, message: EmailMessage) -> EmailReceipt:
        body: dict[str, Any] = {
            "from": message.sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
            "reply_to": message.reply_to,
            "tags": [{"name": k, "value": v} for k, v in message.tags.items()],
        }
        try:
            response = await self._http.post(
                RESEND_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": message.idempotency_key,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise EmailTemporaryError(type(error).__name__) from None
        if response.status_code in _RETRY or response.status_code >= _SERVER:
            raise EmailTemporaryError(f"resend {response.status_code}")
        if response.is_error:
            raise EmailPermanentError(f"resend {response.status_code}")
        try:
            provider_id = str(response.json()["id"])
        except ValueError, KeyError, TypeError:
            raise EmailTemporaryError("resend answered without an id") from None
        return EmailReceipt(provider_id=provider_id)
