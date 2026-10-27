"""Two-factor sign-in, step-up and the session list (slice 3.5 part 2, AC-3.5.3, 3.5.4, 3.5.7)."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import anyio
import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.policy import (
    STEP_UP_WINDOW,
    Capability,
    Outcome,
    Principal,
    ShopRef,
    decide,
)
from htco_application.mfa import (
    MAX_CODE_FAILURES,
    Enrollment,
    Factor,
    MfaCodeRefusedError,
    MfaNotEnrolledError,
    SessionNotFoundError,
    list_sessions,
    mfa_failure_key,
    mfa_status,
    revoke_session,
    start_enrollment,
    step_up,
    verify_enrollment,
)
from htco_application.web_sessions import (
    ActiveSession,
    CredentialsRefusedError,
    IssuedTokens,
    TooManyAttemptsError,
    resume_session,
    sign_in,
)
from htco_domain.identity import AssuranceLevel, Role
from tests.session_fakes import CLIENT, NOW, USER, Auth, Store, Tokens, at_once, tokens

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
SHOP = ShopRef(uuid.UUID("a1000000-0000-0000-0000-000000000001"), ORG)


@dataclass
class Mfa:
    """Supabase MFA in memory: the right code is 123456."""

    factors: list[Factor] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def enroll_totp(self, access_token: str, *, friendly_name: str) -> Enrollment:
        self.calls.append(f"enroll {access_token} {friendly_name}")
        factor = Factor(id=f"factor-{len(self.factors) + 1}", verified=False, name=friendly_name)
        self.factors.append(factor)
        return Enrollment(factor.id, "data:image/svg+xml;utf-8,<svg/>", "SECRET", "otpauth://x")

    async def verify_totp(self, access_token: str, factor_id: str, code: str) -> IssuedTokens:
        self.calls.append(f"verify {access_token} {factor_id}")
        await anyio.lowlevel.checkpoint()  # a network call
        if code != "123456" or factor_id not in {f.id for f in self.factors}:
            raise CredentialsRefusedError
        self.factors = [replace(f, verified=True) if f.id == factor_id else f for f in self.factors]
        return tokens(99)

    async def factors_of(self, access_token: str) -> list[Factor]:  # noqa: ARG002 - fake
        return list(self.factors)

    async def remove_factor(self, access_token: str, factor_id: str) -> None:
        self.calls.append(f"remove {access_token} {factor_id}")
        self.factors = [f for f in self.factors if f.id != factor_id]


async def active(store: Store, auth: Auth, *, at: datetime = NOW) -> ActiveSession:
    signed = await sign_in(
        "owner@example.test",
        auth.password,
        client=CLIENT,
        auth=auth,
        tokens=Tokens(),
        store=store,
        now=NOW,
    )
    return await resume_session(
        signed.cookie_value, auth=auth, tokens=Tokens(), store=store, now=at
    )


async def test_enroll_then_verify__session_gets_the_new_aal2_tokens_and_a_step_up_time() -> None:
    store, auth, mfa = Store(), Auth(), Mfa()
    session = await active(store, auth)

    enrollment = await start_enrollment(session, mfa, friendly_name="Phone")
    await verify_enrollment(
        session, mfa, store, enrollment.factor_id, "123456", client=CLIENT, now=NOW
    )

    stored = store.sessions[session.session_id]
    assert stored.tokens == tokens(99)
    assert stored.reauthenticated_at == NOW
    assert mfa.calls == ["enroll access-1 Phone", "verify access-1 factor-1"]


async def test_enroll_again__the_unfinished_factor_is_removed_first() -> None:
    """Supabase refuses a second factor with the same name, so an abandoned QR code would block
    every later attempt (mfa_factor_name_conflict)."""
    store, auth, mfa = Store(), Auth(), Mfa()
    session = await active(store, auth)
    await start_enrollment(session, mfa, friendly_name="Phone")

    again = await start_enrollment(session, mfa, friendly_name="Phone")

    assert mfa.calls == [
        "enroll access-1 Phone",
        "remove access-1 factor-1",
        "enroll access-1 Phone",
    ]
    assert [(f.id, f.verified) for f in mfa.factors] == [(again.factor_id, False)]


async def test_enroll_again__removes_only_an_unfinished_authenticator_app() -> None:
    """Codex on #171: the cleanup removes the user's unfinished authenticator apps only, never a
    passkey being created at the same time (unverified webauthn); its name stays taken."""
    store, auth, mfa = Store(), Auth(), Mfa()
    mfa.factors = [
        Factor(id="app-old", verified=False, name="Old phone", kind="totp"),
        Factor(id="pk-new", verified=False, name="Phone", kind="webauthn"),
    ]
    session = await active(store, auth)

    await start_enrollment(session, mfa, friendly_name="Phone")

    assert mfa.calls == ["remove access-1 app-old", "enroll access-1 Phone (2)"]
    assert "pk-new" in [f.id for f in mfa.factors]


async def test_enroll_another__a_verified_factor_is_kept_and_the_name_made_unique() -> None:
    store, auth, mfa = Store(), Auth(), Mfa()
    mfa.factors = [Factor(id="kept", verified=True, name="Phone")]
    session = await active(store, auth)

    await start_enrollment(session, mfa, friendly_name="Phone")

    assert mfa.calls == ["enroll access-1 Phone (2)"]
    assert [(f.id, f.verified) for f in mfa.factors] == [("kept", True), ("factor-2", False)]


async def test_verify__wrong_code__refused_and_throttled() -> None:
    store, auth, mfa = Store(), Auth(), Mfa()
    session = await active(store, auth)
    enrollment = await start_enrollment(session, mfa, friendly_name="Phone")

    for _ in range(MAX_CODE_FAILURES):
        with pytest.raises(MfaCodeRefusedError):
            await verify_enrollment(
                session, mfa, store, enrollment.factor_id, "000000", client=CLIENT, now=NOW
            )
    with pytest.raises(TooManyAttemptsError):
        await verify_enrollment(
            session, mfa, store, enrollment.factor_id, "123456", client=CLIENT, now=NOW
        )


async def test_status__assurance_and_factors() -> None:
    store, auth, mfa = Store(), Auth(), Mfa(factors=[Factor("f1", verified=True, name="Phone")])
    session = await active(store, auth)

    status = await mfa_status(session, mfa)

    assert status.assurance is AssuranceLevel.AAL1
    assert status.factors == [Factor("f1", verified=True, name="Phone")]
    assert status.enrolled


async def test_step_up__uses_the_verified_factor() -> None:
    """AC-3.5.7: re-entering the code marks the session as recently re-authenticated."""
    store, auth = Store(), Auth()
    mfa = Mfa(
        factors=[
            Factor("f0", verified=False, name="Old"),
            Factor("f1", verified=True, name="Phone"),
        ]
    )
    session = await active(store, auth)

    await step_up(session, mfa, store, "123456", client=CLIENT, now=NOW)

    assert mfa.calls == ["verify access-1 f1"]
    assert store.sessions[session.session_id].reauthenticated_at == NOW


async def test_step_up__a_burst_of_wrong_codes__only_the_limit_is_verified() -> None:
    """Each code is reserved against the user's limit before Supabase verifies it."""
    store, auth = Store(), Auth()
    mfa = Mfa(factors=[Factor("f1", verified=True, name="Phone")])
    session = await active(store, auth)

    async def wrong(_: int) -> None:
        await step_up(session, mfa, store, "000000", client=CLIENT, now=NOW)

    outcomes = await at_once(MAX_CODE_FAILURES * 3, wrong)

    assert outcomes.count("MfaCodeRefusedError") == MAX_CODE_FAILURES
    assert outcomes.count("TooManyAttemptsError") == MAX_CODE_FAILURES * 2
    assert mfa.calls.count("verify access-1 f1") == MAX_CODE_FAILURES


async def test_step_up__right_code__recorded_as_a_success_not_a_failure() -> None:
    store, auth = Store(), Auth()
    mfa = Mfa(factors=[Factor("f1", verified=True, name="Phone")])
    session = await active(store, auth)

    await step_up(session, mfa, store, "123456", client=CLIENT, now=NOW)

    key = mfa_failure_key(USER)
    assert [a[2] for a in store.attempts if a[0] == key] == [True]
    assert await store.recent_failures(key, None, NOW - timedelta(minutes=15)) == (0, 0)


async def test_step_up__without_a_verified_factor__refused() -> None:
    store, auth = Store(), Auth()
    session = await active(store, auth)
    with pytest.raises(MfaNotEnrolledError):
        await step_up(session, Mfa(), store, "123456", client=CLIENT, now=NOW)


async def test_step_up_window__the_policy_requires_it_and_it_expires_after_ten_minutes() -> None:
    """AC-3.5.7, with an injected clock: connecting a shop needs MFA and a recent step-up."""

    def owner(reauthenticated_at: datetime | None) -> Principal:
        return AuthContext(
            user_id=USER,
            assurance=AssuranceLevel.AAL2,
            session_id=None,
            memberships=(Membership(ORG, Role.OWNER, frozenset()),),
            reauthenticated_at=reauthenticated_at,
        ).principal_for(ORG)

    never = decide(owner(None), Capability.CONNECT_SHOP, shop=SHOP, now=NOW)
    fresh = decide(owner(NOW), Capability.CONNECT_SHOP, shop=SHOP, now=NOW + timedelta(minutes=9))
    stale = decide(owner(NOW), Capability.CONNECT_SHOP, shop=SHOP, now=NOW + STEP_UP_WINDOW)

    assert never.outcome is Outcome.STEP_UP_REQUIRED
    assert fresh.outcome is Outcome.ALLOW
    assert stale.outcome is Outcome.STEP_UP_REQUIRED


async def test_sessions__lists_only_the_users_active_sessions_and_marks_this_one() -> None:
    store, auth = Store(), Auth()
    first = await active(store, auth)
    second = await active(store, auth)
    await store.revoke(first.session_id, NOW)
    third = await active(store, auth)

    sessions = await list_sessions(third, store, now=NOW)

    assert {s.id for s in sessions} == {second.session_id, third.session_id}
    assert [s.current for s in sessions if s.id == third.session_id] == [True]


async def test_revoke_session__another_device__signed_out_there_too() -> None:
    """AC-3.5.4: revoking from this device ends that device's session on its next request."""
    store, auth = Store(), Auth()
    other = await active(store, auth)
    this = await active(store, auth)

    await revoke_session(this, store, auth, other.session_id, now=NOW)

    assert store.sessions[other.session_id].revoked_at == NOW
    assert auth.signed_out == ["access-1"]


async def test_revoke_session__someone_elses_or_unknown__not_found() -> None:
    store, auth = Store(), Auth()
    this = await active(store, auth)
    stranger = replace(store.sessions[this.session_id], id=uuid.uuid4(), user_id=uuid.uuid4())
    store.sessions[stranger.id] = stranger

    for target in (stranger.id, uuid.uuid4()):
        with pytest.raises(SessionNotFoundError):
            await revoke_session(this, store, auth, target, now=NOW)
    assert store.sessions[stranger.id].revoked_at is None
