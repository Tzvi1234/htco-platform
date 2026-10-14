"""A route dependency that asks the policy engine before the route runs (slices 2.4 and 3.5).

`require_capability(Capability.X)` resolves the caller, then decides for the organization named in
the path (`organization_id`), or the caller's only organization. Each refusal has its own problem
type, so the web app knows what to do next: `forbidden`, `mfa-required` (enroll or sign in with
the second factor) or `step-up-required` (re-enter the code, AC-3.5.7).
"""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request

from htco_api.auth import require_auth
from htco_api.problems import ProblemError, ProblemType
from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, decide

FORBIDDEN = ProblemType("forbidden", "Forbidden", 403, "Your role does not allow this.")
MFA_REQUIRED = ProblemType(
    "mfa-required",
    "Second factor required",
    403,
    "This needs a session signed in with your authenticator app.",
)
STEP_UP_REQUIRED = ProblemType(
    "step-up-required",
    "Confirm it is you",
    403,
    "Enter the code from your authenticator app to continue.",
)
_PROBLEM_FOR = {
    Outcome.DENY: FORBIDDEN,
    Outcome.MFA_REQUIRED: MFA_REQUIRED,
    Outcome.STEP_UP_REQUIRED: STEP_UP_REQUIRED,
}


def require_capability(
    capability: Capability, organization_id: uuid.UUID | None = None
) -> Callable[..., Awaitable[AuthContext]]:
    async def dependency(request: Request) -> AuthContext:
        auth = await require_auth(request, None)
        named = organization_id or request.path_params.get("organization_id")
        organization = uuid.UUID(str(named)) if named else auth.default_organization()
        if organization is None:
            raise ProblemError(FORBIDDEN)
        try:
            principal = auth.principal_for(organization)
        except NoMembershipError:
            raise ProblemError(FORBIDDEN) from None
        decision = decide(principal, capability, shop=None, now=request.app.state.clock())
        if decision.outcome is not Outcome.ALLOW:
            raise ProblemError(_PROBLEM_FOR[decision.outcome])
        return auth

    return dependency
