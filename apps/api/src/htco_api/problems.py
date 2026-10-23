"""RFC 9457 problem details: the one error envelope of the API (ER-CODE-06, ASVS 16.5.1).

Every error response is `application/problem+json` with a stable `type` URI, a short `title`,
the HTTP `status`, a human-readable `detail` and the `request_id` that ties it to the logs.
Details are written here, never copied from exceptions, so internals cannot leak.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import compile_path

PROBLEM_TYPE_BASE = "https://ht-co.app/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    """Problem details (RFC 9457) plus the request ID."""

    type: str
    title: str
    status: int
    detail: str
    request_id: str | None


class FieldError(BaseModel):
    """One invalid request field: where it is and what is wrong. The input is never echoed."""

    location: list[str | int]
    message: str


class ValidationProblem(Problem):
    errors: list[FieldError]


class ShopNotConnectedProblem(Problem):
    """`shop-not-connected` says why: `needs_reauth` (reconnect the shop: Etsy refused its tokens,
    or they belong to a replaced app key), `disconnected` or `not_connected`. Other problems on
    the same status carry no reason."""

    reason: str | None = None


@dataclass(frozen=True)
class ProblemType:
    slug: str
    title: str
    status: int
    detail: str

    @property
    def uri(self) -> str:
        return PROBLEM_TYPE_BASE + self.slug

    def problem(self, request_id: str | None) -> Problem:
        return Problem(
            type=self.uri,
            title=self.title,
            status=self.status,
            detail=self.detail,
            request_id=request_id,
        )


NOT_FOUND = ProblemType("not-found", "Not found", 404, "The requested resource does not exist.")
METHOD_NOT_ALLOWED = ProblemType(
    "method-not-allowed", "Method not allowed", 405, "The resource does not support this method."
)
VALIDATION_ERROR = ProblemType(
    "validation-error", "Invalid request", 422, "The request has missing or invalid fields."
)
INTERNAL_ERROR = ProblemType(
    "internal-error",
    "Internal error",
    500,
    "Something went wrong on our side. Quote the request ID if you report it.",
)
# Every route can fail unexpectedly; the middleware then answers with this problem.
UNEXPECTED_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {
        "model": Problem,
        "content": {PROBLEM_MEDIA_TYPE: {}},
        "description": "Unexpected error. Quote the request ID when reporting it",
    }
}
NOT_READY = ProblemType("not-ready", "Not ready", 503, "A required dependency is not available.")

# Authentication (slice 2.3, AC-2.3.1): one type per reason a request is not signed in, so clients
# can tell an expired session (refresh it) from a token that will never work.
UNAUTHENTICATED = ProblemType(
    "unauthenticated", "Not signed in", 401, "Send an access token as `Authorization: Bearer …`."
)
TOKEN_EXPIRED = ProblemType(
    "token-expired", "Session expired", 401, "The access token has expired. Refresh it and retry."
)
TOKEN_WRONG_AUDIENCE = ProblemType(
    "token-wrong-audience",
    "Token not for this API",
    401,
    "The access token is for another audience.",
)
TOKEN_WRONG_ISSUER = ProblemType(
    "token-wrong-issuer",
    "Token from another issuer",
    401,
    "The access token was not issued by HTCO's sign-in service.",
)
TOKEN_BAD_SIGNATURE = ProblemType(
    "token-bad-signature",
    "Token signature invalid",
    401,
    "The access token's signature cannot be verified.",
)
TOKEN_INVALID = ProblemType(
    "token-invalid", "Token invalid", 401, "The access token is not a valid user token."
)
NO_MEMBERSHIP = ProblemType(
    "no-membership", "No access", 403, "Your account is not a member of this organization."
)
AUTH_UNAVAILABLE = ProblemType(
    "auth-unavailable",
    "Sign-in unavailable",
    503,
    "Sign-in cannot be verified right now. Try again shortly.",
)


class ProblemError(Exception):
    """Raised by routes and dependencies to answer with one problem type."""

    def __init__(
        self,
        kind: ProblemType,
        *,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(kind.slug)
        self.kind = kind
        self.headers = dict(headers or {})
        # Extension members (RFC 9457 section 3.2), written by the route, never from an exception.
        self.extra = dict(extra or {})


_BY_STATUS = {kind.status: kind for kind in (NOT_FOUND, METHOD_NOT_ALLOWED, INTERNAL_ERROR)}


def problem_type_for_status(status: int) -> ProblemType:
    """The problem type for an HTTP error raised by the framework (404, 405, 400, 413…)."""
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    phrase = HTTPStatus(status).phrase
    return ProblemType(phrase.lower().replace(" ", "-"), phrase, status, f"{phrase}.")


def problem_response(problem: Problem, *, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def request_id_of(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    # Registered for HTTPException only; anything else fails safe as an internal error.
    if not isinstance(exc, StarletteHTTPException):
        return problem_response(INTERNAL_ERROR.problem(request_id_of(request)))
    problem = problem_type_for_status(exc.status_code).problem(request_id_of(request))
    headers = dict(exc.headers or {})
    if exc.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        headers["Allow"] = _allowed_methods(request) or headers.get("Allow", "")
    return problem_response(problem, headers=headers)


_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _allowed_methods(request: Request) -> str:
    """Every documented method on this path (RFC 9110 §15.5.6).

    Starlette answers 405 from the first route whose path matches, naming only that route's
    methods; a path served by several routes (PATCH and DELETE) needs all of them. The app's own
    OpenAPI document lists them per path template.
    """
    openapi = getattr(request.app, "openapi", None)
    if openapi is None:
        return ""
    methods: set[str] = set()
    for template, operations in openapi().get("paths", {}).items():
        pattern, _, _ = compile_path(template)
        if pattern.match(request.url.path):
            methods.update(method.upper() for method in operations if method in _HTTP_METHODS)
    return ", ".join(sorted(methods))


async def _problem_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProblemError):
        return problem_response(INTERNAL_ERROR.problem(request_id_of(request)))
    problem = exc.kind.problem(request_id_of(request))
    if exc.extra:
        return JSONResponse(
            {**problem.model_dump(mode="json"), **exc.extra},
            status_code=problem.status,
            media_type=PROBLEM_MEDIA_TYPE,
            headers=exc.headers,
        )
    return problem_response(problem, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    base = VALIDATION_ERROR.problem(request_id_of(request))
    details = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = [FieldError(location=list(error["loc"]), message=error["msg"]) for error in details]
    return problem_response(ValidationProblem(**base.model_dump(), errors=errors))


def add_problem_handlers(app: FastAPI) -> None:
    """Answer framework errors (unknown route, wrong method, invalid input) with problems."""
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(ProblemError, _problem_error)
