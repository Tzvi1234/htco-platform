"""Web sessions for the BFF (slice 3.5, AC-3.5.1, AC-3.5.4, AC-3.5.5)."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from htco_application.web_sessions import (
    ABSOLUTE_LIFETIME,
    IDLE_LIFETIME,
    MAX_FAILURES_PER_EMAIL,
    MAX_FAILURES_PER_IP,
    SessionEndedError,
    SignInRefusedError,
    TooManyAttemptsError,
    resume_session,
    sign_in,
    sign_out,
)
from tests.session_fakes import CLIENT, NOW, USER, Auth, Store, Tokens, tokens

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
