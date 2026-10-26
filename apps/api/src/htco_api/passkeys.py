"""Passkeys as a second factor, and removing a factor (issue #113).

- `POST /v1/auth/mfa/passkeys`: start adding a passkey (an `aal2` session); answers the options
  for `navigator.credentials.create`.
- `POST /v1/auth/mfa/passkeys/{factor_id}/verify`: finish adding it with the browser's credential.
- `POST /v1/auth/mfa/passkeys/challenge`: the options for `navigator.credentials.get`, for the
  sign-in's second step or a step-up.
- `POST /v1/auth/step-up/passkey`: confirm with a passkey; the session becomes `aal2`.
- `DELETE /v1/auth/mfa/factors/{factor_id}`: remove a passkey or an authenticator app (an `aal2`
  session; never the last factor nor the last app).

Passkeys are off unless the server is configured for them (`HTCO_PASSKEYS_ENABLED`); then every
passkey route answers 404 `passkeys-unavailable` and `GET /v1/auth/mfa` says `passkeys: false`.
The options and the credential pass through as opaque JSON and are never logged.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from htco_api import notices
from htco_api.policy_guard import STEP_UP_REQUIRED
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType
from htco_api.web_auth import (
    MFA_NOT_ENROLLED,
    WebAuth,
    client_info,
    mfa_provider_of,
    problem_doc,
    resume,
    too_many,
    validation_doc,
    web_auth_of,
)
from htco_application.passkeys import (
    FactorNotFoundError,
    LastFactorError,
    PasskeyChallenge,
    PasskeyRefusedError,
    Passkeys,
    PasskeysUnavailableError,
    StepUpForFactorsError,
)
from htco_application.web_sessions import TooManyAttemptsError

PASSKEYS_UNAVAILABLE = ProblemType(
    "passkeys-unavailable",
    "Passkeys not available",
    404,
    "Biometric sign-in is not available here. Use your authenticator app.",
)
PASSKEY_REFUSED = ProblemType(
    "passkey-refused",
    "Passkey not accepted",
    401,
    "The passkey was not accepted. Try again, or use a code from your authenticator app.",
)
LAST_FACTOR = ProblemType(
    "last-factor",
    "Keep one authenticator app",
    409,
    "Keep at least one authenticator app: it is how you sign in when a device is lost.",
)

# A WebAuthn credential is a few kilobytes at most.
_MAX_CREDENTIAL_BYTES = 16_384
_FACTOR_ID = Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$")


class PasskeyStartRequest(BaseModel):
    name: str = Field(default="Passkey", min_length=1, max_length=60)


class PasskeyOptionsView(BaseModel):
    factor_id: str
    challenge_id: str
    ceremony: str
    # For `navigator.credentials.create/get`, exactly as Supabase Auth made them.
    options: dict[str, Any]


class _Credential(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=100)
    # The browser's `PublicKeyCredential` as JSON (`toJSON()`), passed through to Supabase Auth.
    credential: dict[str, Any] = Field(repr=False)

    @field_validator("credential")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > _MAX_CREDENTIAL_BYTES:
            raise ValueError("the credential is too large")
        return value


class PasskeyVerifyRequest(_Credential):
    pass


class PasskeyConfirmRequest(_Credential):
    factor_id: str = Field(min_length=1, max_length=100)


class AssuranceView(BaseModel):
    assurance: str


def _options(challenge: PasskeyChallenge) -> PasskeyOptionsView:
    return PasskeyOptionsView(
        factor_id=challenge.factor_id,
        challenge_id=challenge.challenge_id,
        ceremony=challenge.ceremony.value,
        options=dict(challenge.options),
    )


def passkeys_of(web: WebAuth) -> Passkeys:
    """The passkey use cases for this server (the MFA routes' own 503 when MFA is not wired)."""
    return Passkeys(
        mfa=mfa_provider_of(web),
        provider=web.passkeys,
        store=web.store,
        audit=web.factor_audit,
        origins=web.allowed_origins,
    )


_REFUSALS: dict[type[Exception], ProblemType] = {
    PasskeysUnavailableError: PASSKEYS_UNAVAILABLE,
    PasskeyRefusedError: PASSKEY_REFUSED,
    StepUpForFactorsError: STEP_UP_REQUIRED,
    LastFactorError: LAST_FACTOR,
}

_COMMON: dict[int | str, dict[str, Any]] = {
    401: problem_doc("Not signed in, or the passkey was refused"),
    403: problem_doc("Not from the web app, or confirm with your second factor first"),
    404: problem_doc("Passkeys are not available here, or no such factor"),
    429: problem_doc("Too many refused passkeys or codes; see Retry-After"),
    503: problem_doc("Sign-in is unavailable"),
}


def _refused(error: Exception) -> ProblemError:
    if isinstance(error, TooManyAttemptsError):
        return too_many()
    if isinstance(error, FactorNotFoundError):
        return ProblemError(NOT_FOUND)
    return ProblemError(_REFUSALS[type(error)])


_EXPECTED = (
    PasskeysUnavailableError,
    PasskeyRefusedError,
    StepUpForFactorsError,
    LastFactorError,
    FactorNotFoundError,
    TooManyAttemptsError,
)


def passkeys_router(*, clock: Any) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["auth"])
    Web = Annotated[WebAuth, Depends(web_auth_of)]  # noqa: N806 - a type alias

    @router.post(
        "/mfa/passkeys",
        summary="Start adding a passkey (biometric sign-in)",
        responses={**_COMMON, 422: validation_doc("Invalid name")},
    )
    async def start_passkey(
        body: PasskeyStartRequest, request: Request, response: Response, web: Web
    ) -> PasskeyOptionsView:
        """Needs a session signed in with two factors; answers the options to create it."""
        active = await resume(request, web, clock())
        try:
            challenge = await passkeys_of(web).start(active, friendly_name=body.name)
        except _EXPECTED as error:
            raise _refused(error) from None
        response.headers["Cache-Control"] = "no-store"
        return _options(challenge)

    @router.post(
        "/mfa/passkeys/{factor_id}/verify",
        summary="Finish adding the passkey",
        responses={**_COMMON, 422: validation_doc("Invalid challenge or credential")},
    )
    async def verify_passkey(
        factor_id: Annotated[str, _FACTOR_ID],
        body: PasskeyVerifyRequest,
        request: Request,
        web: Web,
    ) -> AssuranceView:
        """Checks the new passkey; it is then a second factor (audited)."""
        active = await resume(request, web, clock())
        try:
            await passkeys_of(web).finish(
                active,
                factor_id,
                body.challenge_id,
                body.credential,
                client=client_info(request),
                now=clock(),
            )
        except _EXPECTED as error:
            raise _refused(error) from None
        await notices.mfa_enrolled(request, active.user.user_id, factor_id)
        return AssuranceView(assurance="aal2")

    @router.post(
        "/mfa/passkeys/challenge",
        summary="The options to confirm with a passkey",
        responses={**_COMMON, 409: problem_doc("No passkey is set up")},
    )
    async def challenge_passkey(
        request: Request, response: Response, web: Web
    ) -> PasskeyOptionsView:
        """For the sign-in's second step or a step-up; any passkey of the user will do."""
        active = await resume(request, web, clock())
        try:
            challenge = await passkeys_of(web).challenge(active)
        except FactorNotFoundError:
            raise ProblemError(MFA_NOT_ENROLLED) from None
        except _EXPECTED as error:
            raise _refused(error) from None
        response.headers["Cache-Control"] = "no-store"
        return _options(challenge)

    @router.post(
        "/step-up/passkey",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Confirm it is you with a passkey",
        responses={**_COMMON, 422: validation_doc("Invalid challenge or credential")},
    )
    async def confirm_with_passkey(
        body: PasskeyConfirmRequest, request: Request, web: Web
    ) -> Response:
        """The session becomes `aal2` and counts as re-authenticated for ten minutes."""
        active = await resume(request, web, clock())
        try:
            await passkeys_of(web).confirm(
                active,
                body.factor_id,
                body.challenge_id,
                body.credential,
                client=client_info(request),
                now=clock(),
            )
        except _EXPECTED as error:
            raise _refused(error) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.delete(
        "/mfa/factors/{factor_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        summary="Remove a passkey or an authenticator app",
        responses={**_COMMON, 409: problem_doc("The last factor, or the last authenticator app")},
    )
    async def remove_factor(
        factor_id: Annotated[str, _FACTOR_ID], request: Request, web: Web
    ) -> Response:
        """Needs a session signed in with two factors; the last factor stays (audited)."""
        active = await resume(request, web, clock())
        try:
            removed = await passkeys_of(web).remove(active, factor_id)
        except _EXPECTED as error:
            raise _refused(error) from None
        if removed.verified:
            await notices.mfa_removed(request, active.user.user_id, factor_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
