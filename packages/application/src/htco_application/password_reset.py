"""Password reset for the web app (slice 3.5 part 3, AC-3.5.6).

Asking for a link is admitted (throttled) first and sent afterwards, so the answer is the same
whether or not the email has an account: the auth provider says nothing about it either, and its
failures are only reported back as "not sent". Every request counts against the email (in its
own namespace, so reset requests never lock the email's sign-in) and against the client address,
whose limit is shared with failed sign-ins.

The emailed link lands on the web app with a recovery session's tokens; the new password is set
exactly like accepting an invitation (`set_password_from_link`). A user with an authenticator app
must also enter a code: the auth provider changes the password only from an `aal2` session. Wrong
codes count per user, like at step-up. After the reset, every other session of the user is
ended, here and at the auth provider, since whoever knew the old password may hold one.
"""

import hashlib
from datetime import datetime

from htco_application.access.authentication import AuthenticatedUser
from htco_application.mfa import (
    MAX_CODE_FAILURES,
    MfaProvider,
    SessionDirectory,
    mfa_failure_key,
)
from htco_application.web_sessions import (
    MAX_FAILURES_PER_IP,
    RESET_LINK,
    THROTTLE_WINDOW,
    AuthProvider,
    ClientInfo,
    CredentialsRefusedError,
    Elevate,
    IssuedTokens,
    MfaCodeRefusedError,
    ResetEmailNotSentError,
    SignedIn,
    TokenReader,
    TooManyAttemptsError,
    set_password_from_link,
)

MAX_RESET_REQUESTS_PER_EMAIL = 5


def hash_value(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


async def admit_password_reset(
    email: str, *, client: ClientInfo, store: SessionDirectory, now: datetime
) -> str:
    """Counts the request and returns the normalised email; TooManyAttemptsError past a limit."""
    address = email.strip().lower()
    key = hash_value(f"password-reset-request:{address}")
    ip_hash = hash_value(client.ip) if client.ip else None
    by_email, by_ip = await store.recent_failures(key, ip_hash, now - THROTTLE_WINDOW)
    if by_email >= MAX_RESET_REQUESTS_PER_EMAIL or by_ip >= MAX_FAILURES_PER_IP:
        raise TooManyAttemptsError("too many password reset requests")
    await store.record_attempt(key, ip_hash, succeeded=False, at=now)
    return address


async def send_password_reset(address: str, *, redirect_to: str, auth: AuthProvider) -> bool:
    """Asks the auth provider to email the link; False when it did not."""
    try:
        await auth.send_password_reset(address, redirect_to)
    except ResetEmailNotSentError:
        return False
    return True


async def reset_password(  # noqa: PLR0913 - the link's tokens, the password, the code and the collaborators
    access_token: str,
    refresh_token: str,
    password: str,
    *,
    code: str | None,
    client: ClientInfo,
    auth: AuthProvider,
    mfa: MfaProvider,
    tokens: TokenReader,
    store: SessionDirectory,
    now: datetime,
) -> SignedIn:
    """Sets the new password from a reset link, signs in on this browser and ends every other
    session of the user."""

    def elevate_with(entered: str) -> Elevate:
        async def elevate(link_access_token: str, user: AuthenticatedUser) -> IssuedTokens | None:
            key = mfa_failure_key(user.user_id)
            failures, _ = await store.recent_failures(key, None, now - THROTTLE_WINDOW)
            if failures >= MAX_CODE_FAILURES:
                raise TooManyAttemptsError("too many wrong codes")
            factors = await mfa.factors_of(link_access_token)
            verified = [factor for factor in factors if factor.verified]
            if not verified:
                return None  # no authenticator app: the password changes without a code
            # The request names no factor: the code may come from any of the user's apps (one
            # may be the lost one). One wrong code counts once, however many apps were tried.
            for factor in verified:
                try:
                    return await mfa.verify_totp(link_access_token, factor.id, entered)
                except CredentialsRefusedError:
                    continue
            ip_hash = hash_value(client.ip) if client.ip else None
            await store.record_attempt(key, ip_hash, succeeded=False, at=now)
            raise MfaCodeRefusedError("wrong code")

        return elevate

    signed_in = await set_password_from_link(
        access_token,
        refresh_token,
        password,
        link=RESET_LINK,
        client=client,
        auth=auth,
        tokens=tokens,
        store=store,
        now=now,
        elevate=None if code is None else elevate_with(code),
    )
    for other in await store.list_active(signed_in.user.user_id, now):
        if other.id != signed_in.session_id:
            await store.revoke(other.id, now)
            await auth.sign_out(other.tokens.access_token)
    return signed_in
