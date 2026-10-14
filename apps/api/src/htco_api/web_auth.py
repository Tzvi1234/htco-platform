"""The backend-for-frontend's sign-in for the web app (slice 3.5, ADR-0006).

The browser signs in here and receives one cookie: `__Host-htco_session`, `Secure`, `HttpOnly`,
`SameSite=Strict`, `Path=/`, no `Domain`. Supabase tokens stay on the server (AC-3.5.1). Every
unsafe request that relies on the cookie, and the sign-in itself (login CSRF), must carry the
`X-HTCO-CSRF` header and come from an allowed origin: the `Origin` header when present, otherwise
`Sec-Fetch-Site` (AC-3.5.2). Bearer requests from native apps carry no cookie and skip this.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from htco_api.problems import (
    AUTH_UNAVAILABLE,
    PROBLEM_MEDIA_TYPE,
    Problem,
    ProblemError,
    ProblemType,
)
from htco_application.web_sessions import (
    ABSOLUTE_LIFETIME,
    THROTTLE_WINDOW,
    ActiveSession,
    AuthProvider,
    ClientInfo,
    SessionEndedError,
    SessionStore,
    SignInRefusedError,
    TokenReader,
    TooManyAttemptsError,
    resume_session,
    sign_in,
    sign_out,
)

SESSION_COOKIE = "__Host-htco_session"
CSRF_HEADER = "X-HTCO-CSRF"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAME_SITE = frozenset({"same-origin", "same-site", "none"})
_log = structlog.get_logger(__name__)

SIGN_IN_REFUSED = ProblemType(
    "sign-in-refused", "Sign-in refused", 401, "The email or the password is wrong."
)
TOO_MANY_ATTEMPTS = ProblemType(
    "too-many-attempts",
    "Too many attempts",
    429,
    "Too many failed sign-ins. Wait a few minutes and try again.",
)
CSRF_FAILED = ProblemType(
    "csrf-failed", "Request refused", 403, "This request did not come from the HTCO web app."
)
SESSION_ENDED = ProblemType(
    "session-ended", "Session ended", 401, "Your session ended. Sign in again."
)


@dataclass(frozen=True)
class WebAuth:
    auth: AuthProvider
    tokens: TokenReader
    store: SessionStore
    # The web app's origins, e.g. ("https://app.ht-co.app",).
    allowed_origins: tuple[str, ...]


class SignInRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class SessionView(BaseModel):
    user_id: str
    assurance: str


def web_auth_of(request: Request) -> WebAuth:
    web = getattr(request.app.state, "web_auth", None)
    if not isinstance(web, WebAuth):
        raise ProblemError(AUTH_UNAVAILABLE)
    return web


def check_csrf(request: Request, web: WebAuth) -> None:
    """Refuse a cookie-riding unsafe request that did not come from the web app (AC-3.5.2)."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    from_app = (
        origin in web.allowed_origins
        if origin is not None
        else (fetch_site is None or fetch_site in _SAME_SITE)
    )
    if not request.headers.get(CSRF_HEADER) or not from_app:
        _log.info("auth.csrf_failed", origin=origin, fetch_site=fetch_site)
        raise ProblemError(CSRF_FAILED)


def client_info(request: Request) -> ClientInfo:
    # App Platform's ingress sets `do-connecting-ip`; locally the socket's peer is the client.
    ip = request.headers.get("do-connecting-ip") or (
        request.client.host if request.client else None
    )
    return ClientInfo(ip=ip, user_agent=request.headers.get("user-agent"))


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=int(ABSOLUTE_LIFETIME.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


async def resume(request: Request, web: WebAuth, now: datetime) -> ActiveSession:
    """The signed-in user behind the request's cookie; unsafe methods must pass CSRF first."""
    if request.method in _UNSAFE_METHODS:
        check_csrf(request, web)
    try:
        return await resume_session(
            request.cookies.get(SESSION_COOKIE, ""),
            auth=web.auth,
            tokens=web.tokens,
            store=web.store,
            now=now,
        )
    except SessionEndedError:
        raise ProblemError(SESSION_ENDED, headers={"Set-Cookie": _expired_cookie()}) from None


def _expired_cookie() -> str:
    return f"{SESSION_COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=strict"


def _problem(description: str) -> dict[str, Any]:
    return {"model": Problem, "content": {PROBLEM_MEDIA_TYPE: {}}, "description": description}


def web_auth_router(*, clock: Any) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["auth"])
    Web = Annotated[WebAuth, Depends(web_auth_of)]  # noqa: N806 - a type alias

    @router.post(
        "/sign-in",
        summary="Sign in to the web app",
        responses={
            400: _problem("The body is not valid JSON"),
            401: _problem("Wrong email or password"),
            403: _problem("The request did not come from the web app"),
            422: _problem("Missing email or password"),
            429: _problem("Too many failed sign-ins; see Retry-After"),
            503: _problem("Sign-in is unavailable"),
        },
    )
    async def sign_in_web(
        body: SignInRequest, request: Request, response: Response, web: Web
    ) -> SessionView:
        """Checks the password with Supabase Auth and starts a server-side session."""
        check_csrf(request, web)
        try:
            signed_in = await sign_in(
                body.email,
                body.password,
                client=client_info(request),
                auth=web.auth,
                tokens=web.tokens,
                store=web.store,
                now=clock(),
            )
        except SignInRefusedError:
            raise ProblemError(SIGN_IN_REFUSED) from None
        except TooManyAttemptsError:
            retry = str(int(THROTTLE_WINDOW.total_seconds()))
            raise ProblemError(TOO_MANY_ATTEMPTS, headers={"Retry-After": retry}) from None
        _set_cookie(response, signed_in.cookie_value)
        response.headers["Cache-Control"] = "no-store"
        return SessionView(
            user_id=str(signed_in.user.user_id), assurance=signed_in.user.assurance.value
        )

    @router.get(
        "/session",
        summary="The current web session",
        responses={401: _problem("Not signed in"), 503: _problem("Sign-in is unavailable")},
    )
    async def current_session(request: Request, web: Web) -> SessionView:
        """Who is signed in on this browser, and how strongly."""
        active = await resume(request, web, clock())
        return SessionView(user_id=str(active.user.user_id), assurance=active.user.assurance.value)

    @router.post(
        "/sign-out",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Sign out of the web app",
        responses={403: _problem("The request did not come from the web app")},
    )
    async def sign_out_web(request: Request, web: Web) -> Response:
        """Ends the session here and at Supabase Auth, and clears the cookie."""
        check_csrf(request, web)
        await sign_out(
            request.cookies.get(SESSION_COOKIE, ""), auth=web.auth, store=web.store, now=clock()
        )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_cookie(response)
        return response

    return router
