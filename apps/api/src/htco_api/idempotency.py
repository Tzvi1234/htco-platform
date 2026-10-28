"""`Idempotency-Key` on unsafe routes (slice 5.2, AC-5.2.3): replay the stored response.

A route that opts in wraps its work in `run_idempotently`. With the header, the first request runs
and its response (below 500) is stored for 24 hours; a retry with the same key and body gets that
response again, marked `Idempotent-Replayed: true`, and the work does not run a second time. The
same key with a different body answers 422 `idempotency-key-reused`; a retry while the first is
still running answers 409 `idempotency-in-progress`. Responses that carry buyer contact data are
never stored, so only routes without such data opt in.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse

from htco_api.problems import NOT_READY, ProblemError, ProblemType
from htco_application.idempotency import (
    IdempotencyStore,
    IdempotentRequest,
    InvalidIdempotencyKeyError,
    KeyReused,
    Replay,
    StillRunning,
    check_idempotency_key,
    is_storable,
    request_fingerprint,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

IDEMPOTENCY_KEY_REQUIRED = ProblemType(
    "idempotency-key-required",
    "Idempotency-Key required",
    400,
    "Send an Idempotency-Key header (1 to 255 visible ASCII characters) with this request.",
)
IDEMPOTENCY_KEY_INVALID = ProblemType(
    "idempotency-key-invalid",
    "Invalid Idempotency-Key",
    400,
    "An Idempotency-Key is 1 to 255 visible ASCII characters.",
)
IDEMPOTENCY_KEY_REUSED = ProblemType(
    "idempotency-key-reused",
    "Idempotency-Key reused",
    422,
    "This Idempotency-Key was used with a different request. Use a new key.",
)
# The 422 names the field at fault, so its body is a `ValidationProblem` like every other 422 of
# the routes that opt in (their contract declares that schema, and dropping `errors` from it would
# break clients).
_KEY_REUSED_ERRORS = {
    "errors": [
        {
            "location": ["header", "Idempotency-Key"],
            "message": "Used with a different request; use a new key",
        }
    ]
}
IDEMPOTENCY_IN_PROGRESS = ProblemType(
    "idempotency-in-progress",
    "Request still running",
    409,
    "The first request with this Idempotency-Key has not finished. Retry shortly.",
)

type Work = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]
type Redact = Callable[[dict[str, Any]], dict[str, Any]]

# Architecture audit 6.1, staged: the invitation, re-sync, dead-letter retry and relay paste
# routes replay on an `Idempotency-Key` now and require it once the web client sends it (a
# required header is a breaking contract change, refused by the contract check). Flipping this
# to True is that follow-up: it changes the routes and their OpenAPI together.
STAGED_KEY_REQUIRED = False

# The header as an OpenAPI parameter, for `openapi_extra` of the staged routes.
IDEMPOTENCY_OPENAPI: dict[str, Any] = {
    "parameters": [
        {
            "name": IDEMPOTENCY_HEADER,
            "in": "header",
            "required": STAGED_KEY_REQUIRED,
            "schema": {"type": "string", "minLength": 1, "maxLength": 255},
            "description": "A key for this request; a retry with the same key and body "
            "returns the first response (24 hours). Send it: it becomes required.",
        }
    ]
}


def idempotency_store(request: Request) -> IdempotencyStore:
    """The store the composition root set up; without it, an idempotent route answers 503."""
    store = getattr(request.app.state, "idempotency", None)
    if store is None:
        raise ProblemError(NOT_READY)
    found: IdempotencyStore = store
    return found


def _target(request: Request) -> str:
    """The path and its query (sorted), so the same key with another `?organization_id=` is a
    different request (422), not a replay of the first organization's answer."""
    query = sorted(request.query_params.multi_items())
    return request.url.path if not query else f"{request.url.path}?{urlencode(query)}"


async def run_idempotently(  # noqa: PLR0913 - the request, its caller, the store and the work
    request: Request,
    *,
    user_id: uuid.UUID,
    store: IdempotencyStore,
    now: datetime,
    work: Work,
    required: bool = True,
    redact: Redact | None = None,
) -> JSONResponse:
    """`redact` maps the first response to what is kept for a replay, for a field shown once
    (an invitation link is a bearer credential, never stored)."""
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        if required:
            raise ProblemError(IDEMPOTENCY_KEY_REQUIRED)
        status, body = await work()
        return JSONResponse(body, status_code=status)
    try:
        key = check_idempotency_key(raw)
    except InvalidIdempotencyKeyError:
        raise ProblemError(IDEMPOTENCY_KEY_INVALID) from None
    idempotent = IdempotentRequest(
        user_id=user_id,
        key=key,
        method=request.method,
        path=request.url.path,
        fingerprint=request_fingerprint(request.method, _target(request), await request.body()),
    )
    begun = await store.begin(idempotent, now=now)
    if isinstance(begun, Replay):
        return JSONResponse(
            dict(begun.body), status_code=begun.status, headers={REPLAYED_HEADER: "true"}
        )
    if isinstance(begun, KeyReused):
        raise ProblemError(IDEMPOTENCY_KEY_REUSED, extra=_KEY_REUSED_ERRORS)
    if isinstance(begun, StillRunning):
        raise ProblemError(IDEMPOTENCY_IN_PROGRESS)
    try:
        status, body = await work()
    except BaseException:
        await store.abandon(idempotent)
        raise
    if is_storable(status):
        kept = body if redact is None else redact(dict(body))
        await store.complete(idempotent, status=status, body=kept)
    else:
        await store.abandon(idempotent)
    return JSONResponse(body, status_code=status)
