"""The backend-for-frontend's sign-in for the web app (slice 3.5, ADR-0006).

The browser signs in here and receives one cookie: `__Host-htco_session`, `Secure`, `HttpOnly`,
`SameSite=Strict`, `Path=/`, no `Domain`. Supabase tokens stay on the server (AC-3.5.1). Every
unsafe request that relies on the cookie, and the sign-in itself (login CSRF), must carry the
`X-HTCO-CSRF` header and come from an allowed origin: the `Origin` header when present, otherwise
`Sec-Fetch-Site` (AC-3.5.2). Bearer requests from native apps carry no cookie and skip this.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from htco_api.problems import (
    AUTH_UNAVAILABLE,
    NOT_FOUND,
    PROBLEM_MEDIA_TYPE,
    Problem,
    ProblemError,
    ProblemType,
    ValidationProblem,
)
from htco_application.mfa import (
    MfaCodeRefusedError,
    MfaNotEnrolledError,
    MfaProvider,
    SessionDirectory,
    SessionNotFoundError,
    list_sessions,
    mfa_status,
    revoke_session,
    start_enrollment,
    step_up,
    verify_enrollment,
)
from htco_application.web_sessions import (
    ABSOLUTE_LIFETIME,
    THROTTLE_WINDOW,
    ActiveSession,
    AuthProvider,
    ClientInfo,
    SessionEndedError,
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
    store: SessionDirectory
    # The web app's origins, e.g. ("https://app.ht-co.app",).
    allowed_origins: tuple[str, ...]
    # Authenticator-app enrollment and verification; without it, the MFA routes answer 503.
    mfa: MfaProvider | None = None


MFA_CODE_REFUSED = ProblemType(
    "mfa-code-refused",
    "Wrong code",
    401,
    "The code from your authenticator app is wrong or expired.",
)
MFA_NOT_ENROLLED = ProblemType(
    "mfa-not-enrolled",
    "No authenticator app",
    409,
    "Set up an authenticator app before confirming with a code.",
)


class EnrollRequest(BaseModel):
    name: str = Field(default="Authenticator app", min_length=1, max_length=60)


class EnrollmentView(BaseModel):
    factor_id: str
    qr_code: str
    secret: str
    uri: str


class VerifyRequest(BaseModel):
    factor_id: str = Field(min_length=1, max_length=100)
    code: str = Field(pattern=r"^[0-9]{6}$")


class StepUpRequest(BaseModel):
    code: str = Field(pattern=r"^[0-9]{6}$")


class FactorView(BaseModel):
    id: str
    name: str | None
    verified: bool


class MfaView(BaseModel):
    assurance: str
    enrolled: bool
    factors: list[FactorView]


class AssuranceView(BaseModel):
    assurance: str


class SessionItem(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    current: bool


class SessionList(BaseModel):
    items: list[SessionItem]


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


def _validation(description: str) -> dict[str, Any]:
    return {
        "model": ValidationProblem,
        "content": {PROBLEM_MEDIA_TYPE: {}},
        "description": description,
    }


def web_auth_router(*, clock: Any) -> APIRouter:  # noqa: PLR0915 - one nested function per route
    router = APIRouter(prefix="/v1/auth", tags=["auth"])
    Web = Annotated[WebAuth, Depends(web_auth_of)]  # noqa: N806 - a type alias

    @router.post(
        "/sign-in",
        summary="Sign in to the web app",
        responses={
            400: _problem("The body is not valid JSON"),
            401: _problem("Wrong email or password"),
            403: _problem("The request did not come from the web app"),
            422: _validation("Missing email or password"),
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
            raise _too_many() from None
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

    def mfa_of(web: WebAuth) -> MfaProvider:
        if web.mfa is None:
            raise ProblemError(AUTH_UNAVAILABLE)
        return web.mfa

    @router.get(
        "/mfa",
        summary="Two-factor status of this session",
        responses={401: _problem("Not signed in"), 503: _problem("Sign-in is unavailable")},
    )
    async def get_mfa_status(request: Request, web: Web) -> MfaView:
        """The session's assurance and the user's authenticator apps."""
        active = await resume(request, web, clock())
        status_ = await mfa_status(active, mfa_of(web))
        return MfaView(
            assurance=status_.assurance.value,
            enrolled=status_.enrolled,
            factors=[
                FactorView(id=f.id, name=f.name, verified=f.verified) for f in status_.factors
            ],
        )

    @router.post(
        "/mfa/enroll",
        summary="Start setting up an authenticator app",
        responses={
            400: _problem("The body is not valid JSON"),
            422: _validation("Invalid name or code"),
            401: _problem("Not signed in"),
            403: _problem("The request did not come from the web app"),
            503: _problem("Sign-in is unavailable"),
        },
    )
    async def start_mfa_enrollment(
        body: EnrollRequest, request: Request, response: Response, web: Web
    ) -> EnrollmentView:
        """Returns the QR code (and the secret, for typing it in) to add to the app."""
        active = await resume(request, web, clock())
        enrollment = await start_enrollment(active, mfa_of(web), friendly_name=body.name)
        response.headers["Cache-Control"] = "no-store"
        return EnrollmentView(
            factor_id=enrollment.factor_id,
            qr_code=enrollment.qr_code,
            secret=enrollment.secret,
            uri=enrollment.uri,
        )

    @router.post(
        "/mfa/verify",
        summary="Finish setting up the authenticator app",
        responses={
            400: _problem("The body is not valid JSON"),
            422: _validation("Invalid name or code"),
            401: _problem("Not signed in, or the code is wrong"),
            403: _problem("The request did not come from the web app"),
            429: _problem("Too many wrong codes; see Retry-After"),
            503: _problem("Sign-in is unavailable"),
        },
    )
    async def verify_mfa_enrollment(
        body: VerifyRequest, request: Request, web: Web
    ) -> AssuranceView:
        """Checks the first code; the session is then signed in with two factors."""
        active = await resume(request, web, clock())
        try:
            await verify_enrollment(
                active,
                mfa_of(web),
                web.store,
                body.factor_id,
                body.code,
                client=client_info(request),
                now=clock(),
            )
        except MfaCodeRefusedError:
            raise ProblemError(MFA_CODE_REFUSED) from None
        except TooManyAttemptsError:
            raise _too_many() from None
        return AssuranceView(assurance="aal2")

    @router.post(
        "/step-up",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Confirm it is you with a code",
        responses={
            400: _problem("The body is not valid JSON"),
            422: _validation("Invalid name or code"),
            401: _problem("Not signed in, or the code is wrong"),
            403: _problem("The request did not come from the web app"),
            409: _problem("No authenticator app is set up"),
            429: _problem("Too many wrong codes; see Retry-After"),
            503: _problem("Sign-in is unavailable"),
        },
    )
    async def confirm_step_up(body: StepUpRequest, request: Request, web: Web) -> Response:
        """Re-entering a code allows sensitive actions for the next ten minutes."""
        active = await resume(request, web, clock())
        try:
            await step_up(
                active, mfa_of(web), web.store, body.code, client=client_info(request), now=clock()
            )
        except MfaCodeRefusedError:
            raise ProblemError(MFA_CODE_REFUSED) from None
        except MfaNotEnrolledError:
            raise ProblemError(MFA_NOT_ENROLLED) from None
        except TooManyAttemptsError:
            raise _too_many() from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/sessions",
        summary="This user's signed-in devices",
        responses={401: _problem("Not signed in"), 503: _problem("Sign-in is unavailable")},
    )
    async def list_web_sessions(request: Request, web: Web) -> SessionList:
        """Active sessions, newest activity first; `current` marks this browser."""
        active = await resume(request, web, clock())
        found = await list_sessions(active, web.store, now=clock())
        return SessionList(items=[SessionItem(**vars(item)) for item in found])

    @router.delete(
        "/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Sign out another device",
        responses={
            401: _problem("Not signed in"),
            403: _problem("The request did not come from the web app"),
            404: _problem("No such session of this user"),
            422: _problem("Invalid session ID"),
            503: _problem("Sign-in is unavailable"),
        },
    )
    async def revoke_web_session(session_id: uuid.UUID, request: Request, web: Web) -> Response:
        """Ends that session here and at Supabase; the device is signed out on its next request."""
        active = await resume(request, web, clock())
        try:
            await revoke_session(active, web.store, web.auth, session_id, now=clock())
        except SessionNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _too_many() -> ProblemError:
    return ProblemError(
        TOO_MANY_ATTEMPTS, headers={"Retry-After": str(int(THROTTLE_WINDOW.total_seconds()))}
    )
