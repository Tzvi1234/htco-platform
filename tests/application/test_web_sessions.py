"""Web sessions for the BFF (slice 3.5, AC-3.5.1, AC-3.5.4, AC-3.5.5)."""

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import override

import anyio
import pytest

from htco_application.access.authentication import AuthenticatedUser
from htco_application.web_sessions import (
    ABSOLUTE_LIFETIME,
    IDLE_LIFETIME,
    INVITATION_LINK,
    MAX_FAILURES_PER_EMAIL,
    MAX_FAILURES_PER_IP,
    RESET_LINK,
    ClientInfo,
    InvitationRefusedError,
    IssuedTokens,
    ResetLinkRefusedError,
    SessionEndedError,
    SignInRefusedError,
    TooManyAttemptsError,
    WeakPasswordError,
    accept_invitation,
    redeem_link,
    resume_session,
    sign_in,
    sign_out,
)
from htco_domain.identity import AssuranceLevel
from tests.session_fakes import (
    CLIENT,
    INVITE_ACCESS,
    INVITE_REFRESH,
    NEW_PASSWORD,
    NOW,
    USER,
    Auth,
    Store,
    Tokens,
    at_once,
    tokens,
)

pytestmark = pytest.mark.anyio


async def signed_in(store: Store, auth: Auth, *, at: datetime = NOW) -> str:
    result = await sign_in(
        " Owner@Example.test ",
        auth.password,
        client=CLIENT,
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=at,
    )
    return result.cookie_value


# Sign-in ---------------------------------------------------------------------------------------


async def test_sign_in__creates_a_session_holding_only_a_hash_of_the_cookie() -> None:
    store, auth = Store(), Auth()
    result = await sign_in(
        "owner@example.test",
        auth.password,
        client=CLIENT,
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=NOW,
    )

    [session] = store.sessions.values()
    assert session.token_hash == hashlib.sha256(result.cookie_value.encode()).digest()
    assert result.cookie_value.encode() not in session.token_hash
    assert len(result.cookie_value) >= 43  # 32 random bytes, URL-safe base64
    assert session.user_id == USER
    assert session.tokens == tokens(1)
    assert session.idle_expires_at == NOW + IDLE_LIFETIME
    assert session.absolute_expires_at == NOW + ABSOLUTE_LIFETIME
    assert session.user_agent == "Firefox"
    assert result.user.user_id == USER


async def test_sign_in__wrong_password__refused_and_counted() -> None:
    store, auth = Store(), Auth()
    with pytest.raises(SignInRefusedError):
        await sign_in(
            "owner@example.test",
            "nope",
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )
    assert store.sessions == {}
    assert [a[2] for a in store.attempts] == [False]


async def test_sign_in__unknown_email__same_refusal_as_a_wrong_password() -> None:
    """AC-3.5.5: the answer never reveals whether the email exists."""
    store, auth = Store(), Auth()
    with pytest.raises(SignInRefusedError) as unknown:
        await sign_in(
            "nobody@example.test",
            "x",
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )
    with pytest.raises(SignInRefusedError) as wrong:
        await sign_in(
            "owner@example.test",
            "x",
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )
    assert type(unknown.value) is type(wrong.value)


async def test_sign_in__too_many_failures_for_an_email__throttled_even_when_right() -> None:
    """AC-3.5.5."""
    store, auth = Store(), Auth()
    for _ in range(MAX_FAILURES_PER_EMAIL):
        with pytest.raises(SignInRefusedError):
            await sign_in(
                "owner@example.test",
                "x",
                client=CLIENT,
                auth=auth,
                tokens=Tokens(),
                store=store,
                now=NOW,
            )
    with pytest.raises(TooManyAttemptsError):
        await sign_in(
            "owner@example.test",
            auth.password,
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )
    assert auth.issued == 0  # Supabase was not even asked


async def test_sign_in__too_many_failures_from_an_address__throttled_for_every_email() -> None:
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
        await sign_in(
            "other@example.test",
            "x",
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )


async def test_sign_in__a_burst_for_one_email__only_the_limit_reaches_supabase() -> None:
    """AC-3.5.5: each attempt is reserved before Supabase is asked, so requests in flight count."""
    store, auth = Store(), Auth()

    async def wrong(n: int) -> None:
        await sign_in(
            "owner@example.test",
            "x",
            client=ClientInfo(f"198.51.100.{n}", None),
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    outcomes = await at_once(MAX_FAILURES_PER_EMAIL * 3, wrong)

    assert outcomes.count("SignInRefusedError") == MAX_FAILURES_PER_EMAIL
    assert outcomes.count("TooManyAttemptsError") == MAX_FAILURES_PER_EMAIL * 2


async def test_sign_in__a_burst_from_one_address__only_the_limit_reaches_supabase() -> None:
    store, auth = Store(), Auth()

    async def wrong(n: int) -> None:
        await sign_in(
            f"user{n}@example.test",
            "x",
            client=CLIENT,
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    outcomes = await at_once(MAX_FAILURES_PER_IP + 10, wrong)

    assert outcomes.count("SignInRefusedError") == MAX_FAILURES_PER_IP
    assert outcomes.count("TooManyAttemptsError") == 10


async def test_sign_in__supabase_unreachable__not_counted() -> None:
    class Unreachable(Auth):
        @override
        async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens:
            raise ConnectionError

    store = Store()
    with pytest.raises(ConnectionError):
        await sign_in(
            "owner@example.test",
            "x",
            client=CLIENT,
            auth=Unreachable(),
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    assert store.attempts == []
    assert await store.recent_failures(
        hashlib.sha256(b"owner@example.test").digest(), None, NOW - timedelta(minutes=15)
    ) == (0, 0)


async def test_sign_in__cancelled_while_supabase_answers__the_attempt_stays_counted() -> None:
    """A request dropped in flight fails closed: its reservation counts until the window passes."""
    store = Store()
    with anyio.CancelScope() as scope:

        class Cancelled(Auth):
            @override
            async def sign_in_with_password(self, email: str, password: str) -> IssuedTokens:
                scope.cancel()
                await anyio.lowlevel.checkpoint()
                raise AssertionError  # pragma: no cover - cancelled at the checkpoint

        await sign_in(
            "owner@example.test",
            "x",
            client=CLIENT,
            auth=Cancelled(),
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    assert await store.recent_failures(
        hashlib.sha256(b"owner@example.test").digest(), None, NOW - timedelta(minutes=15)
    ) == (1, 0)


async def test_sign_in__old_failures_no_longer_count() -> None:
    store, auth = Store(), Auth()
    for _ in range(MAX_FAILURES_PER_EMAIL):
        with pytest.raises(SignInRefusedError):
            await sign_in(
                "owner@example.test",
                "x",
                client=CLIENT,
                auth=auth,
                tokens=Tokens(),
                store=store,
                now=NOW,
            )
    later = NOW + timedelta(minutes=16)
    result = await sign_in(
        "owner@example.test",
        auth.password,
        client=CLIENT,
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=later,
    )
    assert result.user.user_id == USER


# Resuming a session ----------------------------------------------------------------------------


async def test_resume__a_fresh_session__returns_the_user_and_keeps_the_tokens() -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)

    active = await resume_session(
        cookie, auth=auth, tokens=Tokens(), store=store, now=NOW + timedelta(minutes=5)
    )

    assert active.user.user_id == USER
    assert active.user.claims == {"t": "access-1"}
    assert auth.issued == 1


async def test_resume__access_token_about_to_expire__refreshed_on_the_server() -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)

    active = await resume_session(
        cookie, auth=auth, tokens=Tokens(), store=store, now=NOW + timedelta(minutes=59, seconds=30)
    )

    assert active.user.claims == {"t": "access-2"}
    [session] = store.sessions.values()
    assert session.tokens.refresh_token == "refresh-2"  # noqa: S105 - test value


async def test_resume__refresh_refused__ends_the_session() -> None:
    """AC-3.5.4: a session revoked elsewhere (its refresh token no longer valid) ends here too."""
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)
    auth.valid_refresh.clear()

    with pytest.raises(SessionEndedError):
        await resume_session(
            cookie, auth=auth, tokens=Tokens(), store=store, now=NOW + timedelta(hours=2)
        )
    [session] = store.sessions.values()
    assert session.revoked_at is not None


@pytest.mark.parametrize(
    "later",
    [IDLE_LIFETIME + timedelta(seconds=1), ABSOLUTE_LIFETIME + timedelta(seconds=1)],
    ids=["idle", "absolute"],
)
async def test_resume__expired_session__ended(later: timedelta) -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)
    if later > IDLE_LIFETIME + timedelta(seconds=1):  # keep it active until the absolute limit
        session = next(iter(store.sessions.values()))
        store.sessions[session.id] = replace(session, idle_expires_at=NOW + ABSOLUTE_LIFETIME * 2)
    with pytest.raises(SessionEndedError):
        await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW + later)


async def test_resume__activity_extends_the_idle_limit_but_not_the_absolute_one() -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)
    at = NOW + IDLE_LIFETIME - timedelta(minutes=1)
    auth.refresh_expires_in = timedelta(days=30)
    await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=at)

    [session] = store.sessions.values()
    assert session.idle_expires_at == at + IDLE_LIFETIME
    assert session.absolute_expires_at == NOW + ABSOLUTE_LIFETIME


@pytest.mark.parametrize("cookie", ["", "unknown-cookie-value"])
async def test_resume__unknown_cookie__ended(cookie: str) -> None:
    with pytest.raises(SessionEndedError):
        await resume_session(cookie, auth=Auth(), tokens=Tokens(), store=Store(), now=NOW)


async def test_resume__revoked_session__ended() -> None:
    """AC-3.5.4: revoking a session from another device ends it on its next request."""
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)
    [session] = store.sessions.values()
    await store.revoke(session.id, NOW)

    with pytest.raises(SessionEndedError):
        await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)


# Sign-out --------------------------------------------------------------------------------------


async def test_sign_out__revokes_here_and_at_supabase() -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)

    await sign_out(cookie, auth=auth, store=store, now=NOW)

    [session] = store.sessions.values()
    assert session.revoked_at == NOW
    assert auth.signed_out == ["access-1"]
    with pytest.raises(SessionEndedError):
        await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)


async def test_sign_out__unknown_or_ended_session__is_quietly_done() -> None:
    await sign_out("nothing", auth=Auth(), store=Store(), now=NOW)


async def test_resume__carries_the_sessions_step_up_time() -> None:
    store, auth = Store(), Auth()
    cookie = await signed_in(store, auth)
    [session] = store.sessions.values()
    await store.set_reauthenticated(session.id, NOW)

    active = await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)

    assert active.user.reauthenticated_at == NOW
    assert active.tokens.access_token == "access-1"  # noqa: S105 - test value


# Accepting an invitation ----------------------------------------------------------------------


async def accept(  # noqa: PLR0913 - the request plus the collaborators
    store: Store,
    auth: Auth,
    access_token: str = INVITE_ACCESS,
    refresh_token: str = INVITE_REFRESH,
    password: str = NEW_PASSWORD,
    *,
    reader: Tokens | None = None,
) -> str:
    result = await accept_invitation(
        access_token,
        refresh_token,
        password,
        client=CLIENT,
        auth=auth,
        tokens=reader or Tokens(),
        store=store,
        now=NOW,
    )
    return result.cookie_value


async def test_accept_invitation__sets_the_password_and_opens_a_session_with_fresh_tokens() -> None:
    store, auth = Store(), Auth()
    auth.invitation()

    cookie = await accept(store, auth)

    assert auth.passwords_set == [(INVITE_ACCESS, NEW_PASSWORD)]
    assert INVITE_REFRESH not in auth.valid_refresh  # refreshed: the link's tokens are spent
    [session] = store.sessions.values()
    assert session.token_hash == hashlib.sha256(cookie.encode()).digest()
    assert session.user_id == USER
    assert session.tokens == tokens(1)
    assert session.idle_expires_at == NOW + IDLE_LIFETIME
    assert session.absolute_expires_at == NOW + ABSOLUTE_LIFETIME
    assert session.user_agent == "Firefox"
    assert [a[2] for a in store.attempts] == [True]
    active = await resume_session(cookie, auth=auth, tokens=Tokens(), store=store, now=NOW)
    assert active.user.user_id == USER


async def test_accept_invitation__invalid_access_token__refused_before_any_change() -> None:
    store, auth = Store(), Auth()
    auth.invitation()
    with pytest.raises(InvitationRefusedError):
        await accept(store, auth, access_token="invalid-token")  # noqa: S106 - test value
    assert auth.passwords_set == []
    assert store.sessions == {}
    assert [a[2] for a in store.attempts] == [False]


async def test_accept_invitation__session_ended_at_supabase__refused() -> None:
    store, auth = Store(), Auth()
    auth.invitation()
    with pytest.raises(InvitationRefusedError):
        await accept(store, auth, access_token="revoked-access")  # noqa: S106 - test value
    assert store.sessions == {}
    assert [a[2] for a in store.attempts] == [False]


async def test_accept_invitation__refresh_token_refused__refused_without_a_session() -> None:
    store, auth = Store(), Auth()
    with pytest.raises(InvitationRefusedError):
        await accept(store, auth, refresh_token="unknown-refresh")  # noqa: S106 - test value
    assert store.sessions == {}
    # The password changed before the refresh failed, so the link stays claimed (a success row).
    assert [a[2] for a in store.attempts] == [True, False]


async def test_accept_invitation__a_burst_with_one_bad_link__only_the_limit_is_tried() -> None:
    """AC-3.5.9: link failures are reserved before the link is checked, so a burst cannot pass."""
    store, auth = Store(), Auth()

    async def bad(n: int) -> None:
        await accept_invitation(
            "invalid-same-link",
            INVITE_REFRESH,
            NEW_PASSWORD,
            client=ClientInfo(f"198.51.100.{n}", None),
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    outcomes = await at_once(MAX_FAILURES_PER_EMAIL * 3, bad)

    assert outcomes.count("InvitationRefusedError") == MAX_FAILURES_PER_EMAIL
    assert outcomes.count("TooManyAttemptsError") == MAX_FAILURES_PER_EMAIL * 2


async def test_accept_invitation__weak_password__reported_and_not_counted_as_a_failure() -> None:
    store, auth = Store(), Auth()
    auth.invitation()
    with pytest.raises(WeakPasswordError):
        await accept(store, auth, password="weak but long enough")  # noqa: S106 - test value
    assert store.sessions == {}
    assert store.attempts == []
    assert INVITE_REFRESH in auth.valid_refresh  # the link's session can try again


async def test_accept_invitation__refreshed_tokens_of_another_user__refused() -> None:
    class Switching(Tokens):
        @override
        async def read(self, access_token: str) -> AuthenticatedUser:
            user = await super().read(access_token)
            if access_token == INVITE_ACCESS:
                return user
            other = uuid.UUID("11111111-0000-0000-0000-00000000000f")
            return AuthenticatedUser(other, AssuranceLevel.AAL1, None, user.claims)

    store, auth = Store(), Auth()
    auth.invitation()
    with pytest.raises(InvitationRefusedError):
        await accept(store, auth, reader=Switching())
    assert store.sessions == {}


async def test_accept_invitation__too_many_failures_from_an_address__throttled() -> None:
    store, auth = Store(), Auth()
    auth.invitation()
    for n in range(MAX_FAILURES_PER_IP):
        with pytest.raises(InvitationRefusedError):
            await accept(store, auth, access_token=f"invalid-{n}")
    with pytest.raises(TooManyAttemptsError):
        await accept(store, auth)
    assert auth.passwords_set == []  # Supabase was not even asked


async def test_accept_invitation__sign_in_failures_from_the_address_count_too() -> None:
    store, auth = Store(), Auth()
    auth.invitation()
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
        await accept(store, auth)


async def test_accept_invitation__repeated_failures_of_one_link__throttled() -> None:
    store, auth = Store(), Auth()
    other_addresses = [ClientInfo(ip=f"198.51.100.{n}", user_agent=None) for n in range(10)]
    for client in other_addresses[:MAX_FAILURES_PER_EMAIL]:
        with pytest.raises(InvitationRefusedError):
            await accept_invitation(
                "invalid-same-link",
                INVITE_REFRESH,
                NEW_PASSWORD,
                client=client,
                auth=auth,
                tokens=Tokens(),
                store=store,
                now=NOW,
            )
    with pytest.raises(TooManyAttemptsError):
        await accept_invitation(
            "invalid-same-link",
            INVITE_REFRESH,
            NEW_PASSWORD,
            client=other_addresses[-1],
            auth=auth,
            tokens=Tokens(),
            store=store,
            now=NOW,
        )


async def test_accept_invitation__replayed_link__refused_before_setting_the_password_again() -> (
    None
):
    store, auth = Store(), Auth()
    auth.invitation()
    await accept(store, auth)
    auth.valid_refresh.add(INVITE_REFRESH)

    with pytest.raises(InvitationRefusedError):
        await accept(store, auth, password="another long passphrase")  # noqa: S106 - test value

    assert auth.passwords_set == [(INVITE_ACCESS, NEW_PASSWORD)]


# Redeeming a link's one-time token on the server (Codex security review, owner gate v4) ---------


async def test_redeem_link__returns_the_links_session_tokens() -> None:
    auth = Auth()

    issued = await redeem_link(
        "invite-hash", link=INVITATION_LINK, client=CLIENT, auth=auth, store=Store(), now=NOW
    )

    assert (issued.access_token, issued.refresh_token) == (INVITE_ACCESS, INVITE_REFRESH)


async def test_redeem_link__refused_token__the_links_refusal_and_counted() -> None:
    store = Store()

    with pytest.raises(ResetLinkRefusedError):
        await redeem_link(
            "forged", link=RESET_LINK, client=CLIENT, auth=Auth(), store=store, now=NOW
        )

    assert [a[2] for a in store.attempts] == [False]


async def test_redeem_link__too_many_refusals_from_one_address__not_asked_again() -> None:
    auth, store = Auth(), Store()
    for n in range(MAX_FAILURES_PER_IP):
        with pytest.raises(InvitationRefusedError):
            await redeem_link(
                f"forged-{n}", link=INVITATION_LINK, client=CLIENT, auth=auth, store=store, now=NOW
            )

    with pytest.raises(TooManyAttemptsError):
        await redeem_link(
            "invite-hash", link=INVITATION_LINK, client=CLIENT, auth=auth, store=store, now=NOW
        )
    assert auth.redeemed == set()


async def test_redeem_link__success__not_counted() -> None:
    store = Store()

    await redeem_link(
        "recovery-hash", link=RESET_LINK, client=CLIENT, auth=Auth(), store=store, now=NOW
    )

    assert store.attempts == []
