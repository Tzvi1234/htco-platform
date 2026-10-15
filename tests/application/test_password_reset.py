"""Password reset for the web app (slice 3.5 part 3, AC-3.5.6).

Asking for a reset link never says whether the email has an account, and is throttled per email
and per address. The link lands on the web app with a recovery session's tokens; the new password
is set exactly like accepting an invitation, and every other session of the user is ended.
"""

import contextlib
import hashlib
import uuid
from dataclasses import replace

import anyio
import pytest

from htco_application.mfa import MAX_CODE_FAILURES
from htco_application.password_reset import (
    MAX_RESET_REQUESTS_PER_EMAIL,
    admit_password_reset,
    reset_password,
    send_password_reset,
)
from htco_application.web_sessions import (
    MAX_FAILURES_PER_EMAIL,
    MAX_FAILURES_PER_IP,
    THROTTLE_WINDOW,
    ClientInfo,
    MfaCodeRefusedError,
    MfaCodeRequiredError,
    ResetLinkRefusedError,
    SamePasswordError,
    SignInRefusedError,
    TooManyAttemptsError,
    WeakPasswordError,
    resume_session,
    sign_in,
)
from tests.session_fakes import (
    CLIENT,
    NEW_PASSWORD,
    NOW,
    RESET_ACCESS,
    RESET_REDIRECT,
    RESET_REFRESH,
    RIGHT_CODE,
    USER,
    Auth,
    Authenticator,
    Store,
    Tokens,
)

pytestmark = pytest.mark.anyio


# Asking for a link -----------------------------------------------------------------------------


async def test_admit__normalises_the_email_and_counts_the_request() -> None:
    store = Store()

    address = await admit_password_reset(
        " Owner@Example.test ", client=CLIENT, store=store, now=NOW
    )

    assert address == "owner@example.test"
    [(key, ip_hash, succeeded, at)] = store.attempts
    assert key == hashlib.sha256(b"password-reset-request:owner@example.test").digest()
    assert ip_hash == hashlib.sha256(CLIENT.ip.encode()).digest()  # type: ignore[union-attr]
    assert (succeeded, at) == (False, NOW)


async def test_admit__too_many_requests_for_one_email__throttled_from_any_address() -> None:
    store = Store()
    for n in range(MAX_RESET_REQUESTS_PER_EMAIL):
        client = ClientInfo(ip=f"198.51.100.{n}", user_agent=None)
        await admit_password_reset("owner@example.test", client=client, store=store, now=NOW)

    with pytest.raises(TooManyAttemptsError):
        await admit_password_reset(
            "OWNER@example.test", client=ClientInfo("192.0.2.1", None), store=store, now=NOW
        )
    assert len(store.attempts) == MAX_RESET_REQUESTS_PER_EMAIL  # the refused one is not counted


async def test_admit__allowed_again_after_the_window() -> None:
    store = Store()
    for _ in range(MAX_RESET_REQUESTS_PER_EMAIL):
        await admit_password_reset("owner@example.test", client=CLIENT, store=store, now=NOW)

    later = NOW + THROTTLE_WINDOW + THROTTLE_WINDOW / 15
    address = await admit_password_reset(
        "owner@example.test", client=CLIENT, store=store, now=later
    )

    assert address == "owner@example.test"


async def test_admit__address_limit_is_shared_with_failed_sign_ins() -> None:
    store, auth = Store(), Auth()
    for n in range(MAX_FAILURES_PER_IP):
        with pytest.raises(SignInRefusedError):
            await sign_in(
                f"user{n}@example.test",
                "x",
                client=CLIENT,
                auth=auth,
                tokens=Tokens(),
                store=store,
                now=NOW,
            )

    with pytest.raises(TooManyAttemptsError):
        await admit_password_reset("someone@example.test", client=CLIENT, store=store, now=NOW)


async def test_admit__requests_do_not_lock_the_emails_sign_in() -> None:
    store, auth = Store(), Auth()
    for _ in range(MAX_RESET_REQUESTS_PER_EMAIL):
        await admit_password_reset("owner@example.test", client=CLIENT, store=store, now=NOW)

    signed = await sign_in(
        "owner@example.test",
        auth.password,
        client=ClientInfo("192.0.2.9", None),
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=NOW,
    )

    assert signed.user.user_id == USER


async def test_send__asks_the_provider_with_the_redirect() -> None:
    auth = Auth()

    sent = await send_password_reset("owner@example.test", redirect_to=RESET_REDIRECT, auth=auth)

    assert sent is True
    assert auth.reset_emails == [("owner@example.test", RESET_REDIRECT)]


async def test_send__provider_failure__reported_as_not_sent() -> None:
    auth = Auth(reset_email_fails=True)

    sent = await send_password_reset("owner@example.test", redirect_to=RESET_REDIRECT, auth=auth)

    assert sent is False


# Setting the new password ----------------------------------------------------------------------


async def reset(  # noqa: PLR0913 - the request plus the collaborators
    store: Store,
    auth: Auth,
    access_token: str = RESET_ACCESS,
    refresh_token: str = RESET_REFRESH,
    password: str = NEW_PASSWORD,
    *,
    code: str | None = None,
    mfa: Authenticator | None = None,
    client: ClientInfo = CLIENT,
) -> str:
    result = await reset_password(
        access_token,
        refresh_token,
        password,
        code=code,
        client=client,
        auth=auth,
        mfa=mfa or Authenticator(auth, enrolled=False),
        tokens=Tokens(),
        store=store,
        now=NOW,
    )
    return result.cookie_value


async def other_session(store: Store, auth: Auth) -> str:
    signed = await sign_in(
        "owner@example.test",
        auth.password,
        client=ClientInfo("192.0.2.50", "Old browser"),
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=NOW,
    )
    return signed.cookie_value


async def test_reset__sets_the_password_and_opens_a_session_with_fresh_tokens() -> None:
    store, auth = Store(), Auth()
    auth.recovery()

    cookie = await reset(store, auth)

    assert auth.passwords_set == [(RESET_ACCESS, NEW_PASSWORD)]
    assert RESET_REFRESH not in auth.valid_refresh
    [session] = store.sessions.values()
    assert session.user_id == USER
    assert session.token_hash == hashlib.sha256(cookie.encode()).digest()
    active = await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)
    assert active.user.user_id == USER


async def test_reset__ends_every_other_session_of_the_user_here_and_at_supabase() -> None:
    store, auth = Store(), Auth()
    old_cookie = await other_session(store, auth)
    [old] = store.sessions.values()
    auth.recovery()

    cookie = await reset(store, auth)

    assert store.sessions[old.id].revoked_at == NOW
    assert auth.signed_out == [old.tokens.access_token]
    with pytest.raises(Exception, match="no active session"):
        await resume_session(old_cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)
    active = await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)
    assert active.session_id != old.id


async def test_reset__sessions_of_other_users_are_kept() -> None:
    store, auth = Store(), Auth()
    await other_session(store, auth)
    [old] = store.sessions.values()
    someone_else = uuid.UUID("11111111-0000-0000-0000-00000000000e")
    store.sessions[old.id] = replace(old, user_id=someone_else)
    auth.recovery()

    await reset(store, auth)

    assert store.sessions[old.id].revoked_at is None
    assert auth.signed_out == []


async def test_reset__invalid_link__refused_without_any_change() -> None:
    store, auth = Store(), Auth()
    await other_session(store, auth)
    auth.recovery()

    with pytest.raises(ResetLinkRefusedError):
        await reset(store, auth, access_token="invalid-token")  # noqa: S106 - test value

    assert auth.passwords_set == []
    assert all(s.revoked_at is None for s in store.sessions.values())
    assert [a[2] for a in store.attempts] == [True, False]


async def test_reset__used_link__refused() -> None:
    store, auth = Store(), Auth()

    with pytest.raises(ResetLinkRefusedError):
        await reset(store, auth)  # the refresh token was never issued or is already spent

    assert store.sessions == {}


async def test_reset__weak_password__reported_not_counted_and_nothing_revoked() -> None:
    store, auth = Store(), Auth()
    await other_session(store, auth)
    auth.recovery()

    with pytest.raises(WeakPasswordError):
        await reset(store, auth, password="weak but long enough")  # noqa: S106 - test value

    assert all(s.revoked_at is None for s in store.sessions.values())
    assert [a[2] for a in store.attempts] == [True]
    assert RESET_REFRESH in auth.valid_refresh


async def test_reset__repeated_failures_of_one_link__throttled() -> None:
    store, auth = Store(), Auth()
    for n in range(MAX_FAILURES_PER_EMAIL):
        with pytest.raises(ResetLinkRefusedError):
            await reset(
                store,
                auth,
                access_token="invalid-same-link",  # noqa: S106 - test value
                client=ClientInfo(f"198.51.100.{n}", None),
            )

    with pytest.raises(TooManyAttemptsError):
        await reset(
            store,
            auth,
            access_token="invalid-same-link",  # noqa: S106 - test value
            client=ClientInfo("192.0.2.1", None),
        )


async def test_reset__link_failures_are_not_the_invitations() -> None:
    store, auth = Store(), Auth()
    for _ in range(MAX_FAILURES_PER_EMAIL):
        with pytest.raises(ResetLinkRefusedError):
            await reset(store, auth, access_token="invalid-same-link")  # noqa: S106

    [key] = {a[0] for a in store.attempts}
    assert key == hashlib.sha256(b"password-reset:invalid-same-link").digest()


async def test_reset__failures_from_the_address_shared_with_sign_in() -> None:
    store, auth = Store(), Auth()
    auth.recovery()
    for n in range(MAX_FAILURES_PER_IP):
        with pytest.raises(SignInRefusedError):
            await sign_in(
                f"user{n}@example.test",
                "x",
                client=CLIENT,
                auth=auth,
                tokens=Tokens(),
                store=store,
                now=NOW,
            )

    with pytest.raises(TooManyAttemptsError):
        await reset(store, auth)
    assert auth.passwords_set == []


# Users with an authenticator app ---------------------------------------------------------------


async def test_reset__authenticator_user_without_a_code__asked_for_it_and_not_counted() -> None:
    store, auth = Store(), Auth(mfa_enrolled=True)
    auth.recovery()

    with pytest.raises(MfaCodeRequiredError):
        await reset(store, auth, mfa=Authenticator(auth))

    assert auth.passwords_set == []
    assert store.attempts == []
    assert RESET_REFRESH in auth.valid_refresh  # the page can ask for the code and try again


async def test_reset__authenticator_user_with_the_code__password_set_with_the_aal2_session() -> (
    None
):
    store, auth = Store(), Auth(mfa_enrolled=True)
    auth.recovery()
    mfa = Authenticator(auth)

    await reset(store, auth, code=RIGHT_CODE, mfa=mfa)

    assert mfa.verified == [(RESET_ACCESS, "factor-1")]
    assert auth.passwords_set == [("aal2-access", NEW_PASSWORD)]
    assert "aal2-refresh" not in auth.valid_refresh  # exchanged for the session's tokens
    [session] = store.sessions.values()
    assert session.user_id == USER


async def test_reset__wrong_code__refused_and_counted_for_the_link_and_the_user() -> None:
    store, auth = Store(), Auth(mfa_enrolled=True)
    auth.recovery()

    with pytest.raises(MfaCodeRefusedError):
        await reset(store, auth, code="000000", mfa=Authenticator(auth))

    keys = [a[0] for a in store.attempts if not a[2]]
    assert hashlib.sha256(f"mfa:{USER}".encode()).digest() in keys
    assert hashlib.sha256(f"password-reset:{RESET_ACCESS}".encode()).digest() in keys
    assert auth.passwords_set == []


async def test_reset__too_many_wrong_codes_for_the_user__throttled_before_verifying() -> None:
    store, auth = Store(), Auth(mfa_enrolled=True)
    mfa = Authenticator(auth)
    for n in range(MAX_CODE_FAILURES):
        access, _ = auth.recovery()
        with pytest.raises(MfaCodeRefusedError):
            await reset(
                store,
                auth,
                access_token=f"{access}-{n}",
                code="000000",
                mfa=mfa,
                client=ClientInfo(f"198.51.100.{n}", None),
            )

    with pytest.raises(TooManyAttemptsError):
        await reset(store, auth, code=RIGHT_CODE, mfa=mfa, client=ClientInfo("192.0.2.1", None))
    assert mfa.verified == []


async def test_reset__code_for_a_user_without_an_authenticator__ignored() -> None:
    store, auth = Store(), Auth()
    auth.recovery()
    mfa = Authenticator(auth, enrolled=False)

    await reset(store, auth, code=RIGHT_CODE, mfa=mfa)

    assert mfa.verified == []
    assert auth.passwords_set == [(RESET_ACCESS, NEW_PASSWORD)]


async def test_reset__replayed_link__refused_before_any_change() -> None:
    store, auth = Store(), Auth()
    auth.recovery()
    await reset(store, auth)
    auth.valid_refresh.add(RESET_REFRESH)  # even if the refresh token were somehow usable again

    with pytest.raises(ResetLinkRefusedError):
        await reset(store, auth, password="another long passphrase")  # noqa: S106 - test value

    assert auth.passwords_set == [(RESET_ACCESS, NEW_PASSWORD)]
    assert len(store.sessions) == 1


async def test_reset__second_authenticator_app__its_code_works() -> None:
    store, auth = Store(), Auth(mfa_enrolled=True)
    auth.recovery()
    mfa = Authenticator(auth, codes={"factor-1": "111111", "factor-2": RIGHT_CODE})

    await reset(store, auth, code=RIGHT_CODE, mfa=mfa)

    assert mfa.verified == [(RESET_ACCESS, "factor-2")]
    assert auth.passwords_set == [("aal2-access", NEW_PASSWORD)]


async def test_reset__wrong_code_for_every_authenticator__counted_once_for_the_user() -> None:
    store, auth = Store(), Auth(mfa_enrolled=True)
    auth.recovery()
    mfa = Authenticator(auth, codes={"factor-1": "111111", "factor-2": "222222"})

    with pytest.raises(MfaCodeRefusedError):
        await reset(store, auth, code="000000", mfa=mfa)

    user_key = hashlib.sha256(f"mfa:{USER}".encode()).digest()
    assert [a[0] for a in store.attempts if not a[2]].count(user_key) == 1


async def test_reset__the_current_password__reported_and_not_counted() -> None:
    store, auth = Store(), Auth()
    auth.recovery()

    with pytest.raises(SamePasswordError):
        await reset(store, auth, password=auth.password)

    assert store.attempts == []
    assert RESET_REFRESH in auth.valid_refresh


async def test_reset__the_same_link_twice_at_once__only_one_changes_the_password() -> None:
    store, auth = Store(), Auth()
    auth.recovery()
    outcomes: list[str] = []

    async def attempt(password: str) -> None:
        try:
            await reset(store, auth, password=password)
            outcomes.append("reset")
        except ResetLinkRefusedError:
            outcomes.append("refused")

    async with anyio.create_task_group() as group:
        group.start_soon(attempt, "first long passphrase")
        group.start_soon(attempt, "second long passphrase")

    assert sorted(outcomes) == ["refused", "reset"]
    assert len(auth.passwords_set) == 1


async def test_admit__a_burst_at_once__admits_only_the_limit() -> None:
    store = Store()
    admitted: list[str] = []

    async def ask(n: int) -> None:
        client = ClientInfo(ip=f"198.51.100.{n}", user_agent=None)
        with contextlib.suppress(TooManyAttemptsError):
            address = await admit_password_reset(
                "owner@example.test", client=client, store=store, now=NOW
            )
            admitted.append(address)

    async with anyio.create_task_group() as group:
        for n in range(MAX_RESET_REQUESTS_PER_EMAIL * 2):
            group.start_soon(ask, n)

    assert len(admitted) == MAX_RESET_REQUESTS_PER_EMAIL


async def test_reset__refused_refresh_after_the_password_changed__the_link_stays_claimed() -> None:
    store, auth = Store(), Auth()
    auth.recovery()
    with pytest.raises(ResetLinkRefusedError):
        await reset(store, auth, refresh_token="spent-refresh")  # noqa: S106 - test value

    with pytest.raises(ResetLinkRefusedError):
        await reset(
            store,
            auth,
            refresh_token="another-spent-refresh",  # noqa: S106 - test value
            password="another long passphrase",  # noqa: S106 - test value
        )

    assert auth.passwords_set == [(RESET_ACCESS, NEW_PASSWORD)]
