"""`POST /webhooks/etsy` (slice 5.3): Etsy's signed events, verified on the raw body.

Off unless `HTCO_ETSY_WEBHOOKS_ENABLED` is true and `HTCO_ETSY_WEBHOOK_SECRET` is set: the route
then answers 404 like any unknown path. Not signed in and not CSRF-checked (Etsy calls it); the
Standard Webhooks signature is its authentication. What each refusal answers:

The checks run in this order. `do-connecting-ip` may be forged, so the client IP never decides
whether a signed request is checked: the per-IP window counts only failures, and only a verified
delivery spends the Etsy-wide allowance.

| Case | Answer | Etsy retries? |
|---|---|---|
| Missing signature headers (before the body is read) | 401 | yes |
| Body over 64 KiB | 413 | yes (it never fits: the event stays in Etsy's portal) |
| Bad signature or timestamp, outside the 5-minute window | 401 | yes |
| One of the three above, from an IP over its failure window | 429 with `Retry-After` | yes |
| Verified, but over the Etsy-wide rate limit | 429 with `Retry-After` | yes, accepted later |
| Signed, but not an Etsy event (not JSON, no type or shop) | 400 | yes |
| A `webhook-id` already received | 204 (ignored, nothing new is stored) | no |
| Accepted (routed, or stored without a job: unknown type or shop) | 204 | no |

Every refusal is logged with its reason and never with the body or the headers' values.
"""

import math

import structlog
from fastapi import APIRouter, Request, Response

from htco_api.problems import (
    NOT_FOUND,
    PROBLEM_MEDIA_TYPE,
    Problem,
    ProblemError,
    ProblemType,
)
from htco_api.web_auth import client_info
from htco_application.etsy_webhooks import (
    MAX_BODY_BYTES,
    WEBHOOK_SOURCE,
    EtsyWebhookIngress,
    Rejection,
    WebhookRejectedError,
    WindowLimiter,
    has_signature_headers,
    ip_source,
)

_log = structlog.get_logger(__name__)

WEBHOOK_RATE_LIMITED = ProblemType(
    "webhook-rate-limited", "Too many webhooks", 429, "Too many webhooks. Retry after the wait."
)
WEBHOOK_TOO_LARGE = ProblemType(
    "webhook-too-large", "Webhook too large", 413, "The webhook body is larger than 64 KiB."
)
WEBHOOK_SIGNATURE_INVALID = ProblemType(
    "webhook-signature-invalid",
    "Webhook signature invalid",
    401,
    "The webhook's signature, timestamp or headers do not verify.",
)
WEBHOOK_INVALID = ProblemType(
    "webhook-invalid", "Webhook invalid", 400, "The webhook is not an Etsy event."
)

_PROBLEM_FOR = {
    Rejection.TOO_LARGE: WEBHOOK_TOO_LARGE,
    Rejection.BAD_PAYLOAD: WEBHOOK_INVALID,
}


async def _body(request: Request) -> bytes:
    """The raw body, refused as soon as it passes the limit (never read whole first). Without
    the signature headers nothing is read at all."""
    if not has_signature_headers(request.headers):
        raise WebhookRejectedError(Rejection.MISSING_HEADERS)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise WebhookRejectedError(Rejection.TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise WebhookRejectedError(Rejection.TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def webhooks_router() -> APIRouter:
    router = APIRouter(tags=["webhooks"])

    @router.post(
        "/webhooks/etsy",
        status_code=204,
        summary="Receive an Etsy webhook",
        description="Etsy's signed event (Standard Webhooks). Off unless enabled on the server.",
        responses={
            status: {
                "model": Problem,
                "content": {PROBLEM_MEDIA_TYPE: {}},
                "description": description,
            }
            for status, description in {
                400: "Signed, but not an Etsy event (webhook-invalid)",
                401: "Signature, timestamp or headers invalid (webhook-signature-invalid)",
                404: "Webhooks are not enabled on this server (not-found)",
                413: "Body over 64 KiB (webhook-too-large)",
                429: "Over the per-IP failure limit or the Etsy-wide rate limit "
                "(webhook-rate-limited)",
            }.items()
        },
    )
    async def receive_etsy_webhook(request: Request) -> Response:
        state = request.app.state
        ingress = getattr(state, "etsy_webhooks", None)
        if not isinstance(ingress, EtsyWebhookIngress):
            raise ProblemError(NOT_FOUND)
        headers = dict(request.headers)
        try:
            body = await _body(request)
            signed = ingress.verify(headers, body)
        except WebhookRejectedError as rejected:
            # Only a failure is charged to its IP. `do-connecting-ip` may be forged, so a signed
            # request always reaches the HMAC check: a flood under Etsy's IP cannot block Etsy.
            _admit(
                getattr(state, "etsy_webhook_ip_limiter", None),
                ip_source(client_info(request).ip),
                ingress,
                reason="ip_rate_limited",
            )
            raise _refused(rejected) from None
        # Only a verified delivery spends the Etsy-wide allowance (Codex security review P2).
        etsy_wide = getattr(state, "etsy_webhook_limiter", None)
        _admit(etsy_wide, WEBHOOK_SOURCE, ingress, reason="rate_limited")
        try:
            await ingress.accept(signed, body)
        except WebhookRejectedError as rejected:
            raise _refused(rejected) from None
        return Response(status_code=204)

    return router


def _refused(rejected: WebhookRejectedError) -> ProblemError:
    _log.warning("etsy.webhook_rejected", reason=rejected.reason.value)
    return ProblemError(_PROBLEM_FOR.get(rejected.reason, WEBHOOK_SIGNATURE_INVALID))


def _admit(limiter: object, source: str, ingress: EtsyWebhookIngress, *, reason: str) -> None:
    """429 with `Retry-After` when `source` is over its window (no limiter: always admitted)."""
    if not isinstance(limiter, WindowLimiter):
        return
    wait = limiter.admit(source, now=ingress.clock())
    if wait is not None:
        _log.warning("etsy.webhook_rejected", reason=reason)
        raise ProblemError(
            WEBHOOK_RATE_LIMITED, headers={"Retry-After": str(max(1, math.ceil(wait)))}
        )
