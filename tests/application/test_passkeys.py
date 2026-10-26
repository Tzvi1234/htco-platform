"""Passkeys as a second factor next to the authenticator app (issue #113): adding one needs an
`aal2` session, a passkey confirms the sign-in's second step, the last factor (and the last app)
stays, the credential must come from the web app's origin, and changes are audited."""

import base64
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, override

import anyio
import pytest

from htco_application.mfa import MAX_CODE_FAILURES, Factor
from htco_application.passkeys import (
    PASSKEY,
    TOTP,
    Ceremony,
    FactorNotFoundError,
    LastFactorError,
    PasskeyChallenge,
    PasskeyRefusedError,
    Passkeys,
    PasskeysUnavailableError,
    StepUpForFactorsError,
    credential_origin,
)
from htco_application.web_sessions import (
    ActiveSession,
    CredentialsRefusedError,
    IssuedTokens,
    TooManyAttemptsError,
)
from htco_domain.identity import AssuranceLevel
from tests.application.test_mfa import Mfa, active
from tests.session_fakes import CLIENT, NOW, USER, Auth, Store, tokens

pytestmark = pytest.mark.anyio

ORIGIN = "https://dash.ht-co.app"
OPTIONS = {"publicKey": {"challenge": "c2VjcmV0", "rp": {"id": "ht-co.app"}}}


def credential(origin: str = ORIGIN, *, kind: str = "webauthn.get") -> dict[str, Any]:
    client_data = json.dumps({"type": kind, "challenge": "c2VjcmV0", "origin": origin}).encode()
    encoded = base64.urlsafe_b64encode(client_data).rstrip(b"=").decode()
    return {
        "id": "cred-1",
        "rawId": "cred-1",
        "type": "public-key",
        "response": {"clientDataJSON": encoded, "authenticatorData": "AAAA", "signature": "AAAA"},
    }


@dataclass
class Provider:
    """Supabase's WebAuthn factor in memory: `good` credentials pass, others are refused."""

    mfa: Mfa
    calls: list[str] = field(default_factory=list)
    refuse: bool = False

    async def enroll_passkey(self, access_token: str, *, friendly_name: str) -> str:
        self.calls.append(f"enroll {access_token} {friendly_name}")
        factor = Factor(f"pk-{len(self.mfa.factors) + 1}", False, friendly_name, PASSKEY)
        self.mfa.factors.append(factor)
        return factor.id

    async def challenge_passkey(self, access_token: str, factor_id: str) -> PasskeyChallenge:
        self.calls.append(f"challenge {access_token} {factor_id}")
        factor = next(f for f in self.mfa.factors if f.id == factor_id)
        ceremony = Ceremony.REQUEST if factor.verified else Ceremony.CREATE
        return PasskeyChallenge(factor_id, "ch-1", ceremony, OPTIONS)

    async def verify_passkey(
        self,
        access_token: str,
        factor_id: str,
        challenge_id: str,
        ceremony: Ceremony,
        credential: Mapping[str, Any],
    ) -> IssuedTokens:
        self.calls.append(f"verify {access_token} {factor_id} {challenge_id} {ceremony.value}")
        if self.refuse or credential.get("id") != "cred-1":
            raise CredentialsRefusedError
        self.mfa.factors = [
            replace(f, verified=True) if f.id == factor_id else f for f in self.mfa.factors
        ]
        return tokens(77)


@dataclass
class Audit:
    changes: list[tuple[uuid.UUID, str, str, str]] = field(default_factory=list)

    async def factor_changed(
        self, user_id: uuid.UUID, *, factor_id: str, kind: str, change: str
    ) -> None:
        self.changes.append((user_id, factor_id, kind, change))


@dataclass
class World:
    store: Store = field(default_factory=Store)
    auth: Auth = field(default_factory=Auth)
    mfa: Mfa = field(default_factory=Mfa)
    audit: Audit = field(default_factory=Audit)
    switched_on: bool = True

    def __post_init__(self) -> None:
        self.provider = Provider(self.mfa)

    @property
    def passkeys(self) -> Passkeys:
        return Passkeys(
            mfa=self.mfa,
            provider=self.provider if self.switched_on else None,
            store=self.store,
            audit=self.audit,
            origins=(ORIGIN,),
        )

    async def session(self, assurance: AssuranceLevel = AssuranceLevel.AAL2) -> ActiveSession:
        signed = await active(self.store, self.auth)
        return replace(signed, user=replace(signed.user, assurance=assurance))

    def with_app(self) -> World:
        self.mfa.factors.append(Factor("app-1", True, "Authenticator app", TOTP))
        return self


async def test_add__creates_then_checks_the_passkey_which_is_audited() -> None:
    world = World().with_app()
    session = await world.session()

    challenge = await world.passkeys.start(session, friendly_name="MacBook")
    await world.passkeys.finish(
        session, challenge.factor_id, challenge.challenge_id, credential(), client=CLIENT, now=NOW
    )

    assert challenge.ceremony is Ceremony.CREATE
    assert challenge.options == OPTIONS
    assert [f.kind for f in world.mfa.factors if f.verified] == [TOTP, PASSKEY]
    assert world.store.sessions[session.session_id].tokens == tokens(77)
    assert world.audit.changes == [(USER, "pk-2", PASSKEY, "enrolled")]


async def test_add__several_passkeys_each_with_its_own_name() -> None:
    world = World().with_app()
    session = await world.session()
    for _ in range(2):
        started = await world.passkeys.start(session, friendly_name="Phone")
        await world.passkeys.finish(
            session, started.factor_id, started.challenge_id, credential(), client=CLIENT, now=NOW
        )
    names = [f.name for f in world.mfa.factors if f.kind == PASSKEY]
    assert names == ["Phone", "Phone (2)"]


async def test_add__an_unfinished_passkey_is_removed_first() -> None:
    world = World().with_app()
    session = await world.session()
    await world.passkeys.start(session, friendly_name="Phone")  # cancelled in the browser
    await world.passkeys.start(session, friendly_name="Phone")
    assert [f.id for f in world.mfa.factors] == ["app-1", "pk-2"]


@pytest.mark.parametrize("change", ["start", "finish", "remove"])
async def test_step_up__adding_or_removing_needs_an_aal2_session(change: str) -> None:
    world = World().with_app()
    session = await world.session(AssuranceLevel.AAL1)

    async def attempt() -> None:
        if change == "start":
            await world.passkeys.start(session, friendly_name="Phone")
        elif change == "finish":
            await world.passkeys.finish(
                session, "pk-1", "ch-1", credential(), client=CLIENT, now=NOW
            )
        else:
            await world.passkeys.remove(session, "app-1", now=NOW)

    with pytest.raises(StepUpForFactorsError):
        await attempt()
    assert world.provider.calls == []
    assert [f.id for f in world.mfa.factors] == ["app-1"]


async def test_sign_in__a_passkey_confirms_the_second_step_at_aal1() -> None:
    world = World().with_app()
    world.mfa.factors.append(Factor("pk-9", True, "Phone", PASSKEY))
    session = await world.session(AssuranceLevel.AAL1)

    challenge = await world.passkeys.challenge(session)
    await world.passkeys.confirm(
        session, "pk-9", challenge.challenge_id, credential(), client=CLIENT, now=NOW
    )

    assert challenge.ceremony is Ceremony.REQUEST
    stored = world.store.sessions[session.session_id]
    assert stored.tokens == tokens(77)
    assert stored.reauthenticated_at == NOW


async def test_sign_in__without_a_passkey_there_is_nothing_to_challenge() -> None:
    world = World().with_app()
    with pytest.raises(FactorNotFoundError):
        await world.passkeys.challenge(await world.session(AssuranceLevel.AAL1))


async def test_sign_in__a_refused_passkey_counts_like_a_wrong_code() -> None:
    world = World().with_app()
    world.mfa.factors.append(Factor("pk-9", True, "Phone", PASSKEY))
    world.provider.refuse = True
    session = await world.session(AssuranceLevel.AAL1)
    for _ in range(MAX_CODE_FAILURES):
        with pytest.raises(PasskeyRefusedError):
            await world.passkeys.confirm(
                session, "pk-9", "ch-1", credential(), client=CLIENT, now=NOW
            )
    with pytest.raises(TooManyAttemptsError):
        await world.passkeys.confirm(session, "pk-9", "ch-1", credential(), client=CLIENT, now=NOW)


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "https://dash.ht-co.app.evil.example", "http://dash.ht-co.app"],
)
async def test_origin__a_credential_from_another_site_never_reaches_auth(origin: str) -> None:
    """AC-5: only the web app's origin; Auth checks its own RP config again."""
    world = World().with_app()
    world.mfa.factors.append(Factor("pk-9", True, "Phone", PASSKEY))
    session = await world.session(AssuranceLevel.AAL1)
    with pytest.raises(PasskeyRefusedError):
        await world.passkeys.confirm(
            session, "pk-9", "ch-1", credential(origin), client=CLIENT, now=NOW
        )
    assert world.provider.calls == []


@pytest.mark.parametrize(
    "broken",
    [{}, {"response": {}}, {"response": {"clientDataJSON": "%%%"}}, {"response": "x"}],
)
def test_origin__a_malformed_credential_has_none(broken: dict[str, Any]) -> None:
    assert credential_origin(broken) is None


async def test_remove__a_passkey_and_it_is_audited() -> None:
    world = World().with_app()
    world.mfa.factors.append(Factor("pk-9", True, "Phone", PASSKEY))
    removed = await world.passkeys.remove(await world.session(), now=NOW, factor_id="pk-9")
    assert removed.kind == PASSKEY
    assert [f.id for f in world.mfa.factors] == ["app-1"]
    assert world.audit.changes == [(USER, "pk-9", PASSKEY, "removed")]


async def test_remove__the_last_factor_is_blocked() -> None:
    world = World().with_app()
    with pytest.raises(LastFactorError):
        await world.passkeys.remove(await world.session(), now=NOW, factor_id="app-1")
    assert [f.id for f in world.mfa.factors] == ["app-1"]
    assert world.audit.changes == []


async def test_remove__the_last_authenticator_app_stays_even_with_a_passkey() -> None:
    """The app is the recovery for a lost device, and the password reset asks for its code."""
    world = World().with_app()
    world.mfa.factors.append(Factor("pk-9", True, "Phone", PASSKEY))
    with pytest.raises(LastFactorError):
        await world.passkeys.remove(await world.session(), now=NOW, factor_id="app-1")


async def test_remove__a_second_app_may_go() -> None:
    world = World().with_app()
    world.mfa.factors.append(Factor("app-2", True, "Old phone", TOTP))
    await world.passkeys.remove(await world.session(), now=NOW, factor_id="app-2")
    assert [f.id for f in world.mfa.factors] == ["app-1"]


class SlowMfa(Mfa):
    """Supabase over the network: every call yields, so two requests can interleave."""

    @override
    async def factors_of(self, access_token: str) -> list[Factor]:
        await anyio.lowlevel.checkpoint()
        return await super().factors_of(access_token)

    @override
    async def remove_factor(self, access_token: str, factor_id: str) -> None:
        await anyio.lowlevel.checkpoint()
        await super().remove_factor(access_token, factor_id)


async def test_remove__two_apps_removed_at_once__one_app_always_stays() -> None:
    """Codex on #171: two requests removing the user's two apps both saw the other app and
    both went through, leaving only the passkey. Changes are serialized per user."""
    world = World(mfa=SlowMfa()).with_app()
    world.mfa.factors += [
        Factor("app-2", True, "Old phone", TOTP),
        Factor("pk-9", True, "Phone", PASSKEY),
    ]
    session = await world.session()
    outcomes: list[str] = []

    async def remove(factor_id: str) -> None:
        try:
            await world.passkeys.remove(session, factor_id, now=NOW)
            outcomes.append("removed")
        except (LastFactorError, TooManyAttemptsError) as error:
            outcomes.append(type(error).__name__)

    async with anyio.create_task_group() as group:
        group.start_soon(remove, "app-1")
        group.start_soon(remove, "app-2")

    assert outcomes.count("removed") == 1
    assert any(f.kind == TOTP and f.verified for f in world.mfa.factors)


async def test_remove__the_next_change_may_run_once_the_first_finished() -> None:
    world = World().with_app()
    world.mfa.factors += [
        Factor("app-2", True, "Old phone", TOTP),
        Factor("pk-9", True, "Phone", PASSKEY),
    ]
    session = await world.session()

    await world.passkeys.remove(session, "pk-9", now=NOW)
    await world.passkeys.remove(session, "app-2", now=NOW)

    assert [f.id for f in world.mfa.factors] == ["app-1"]


async def test_remove__off_with_the_flag_like_every_passkey_route() -> None:
    world = World(switched_on=False).with_app()
    world.mfa.factors.append(Factor("app-2", True, "Old phone", TOTP))
    with pytest.raises(PasskeysUnavailableError):
        await world.passkeys.remove(await world.session(), "app-2", now=NOW)
    assert len(world.mfa.factors) == 2


async def test_remove__an_unknown_factor() -> None:
    world = World().with_app()
    with pytest.raises(FactorNotFoundError):
        await world.passkeys.remove(await world.session(), now=NOW, factor_id="nope")


async def test_switched_off__every_passkey_step_says_so() -> None:
    world = World(switched_on=False).with_app()
    session = await world.session()
    assert world.passkeys.available is False
    with pytest.raises(PasskeysUnavailableError):
        await world.passkeys.start(session, friendly_name="Phone")
    with pytest.raises(PasskeysUnavailableError):
        await world.passkeys.challenge(session)


async def test_step_up_with_a_code__uses_the_app_even_when_a_passkey_comes_first() -> None:
    from htco_application.mfa import step_up  # noqa: PLC0415

    world = World()
    world.mfa.factors = [
        Factor("pk-9", True, "Phone", PASSKEY),
        Factor("app-1", True, "Authenticator app", TOTP),
    ]
    session = await world.session(AssuranceLevel.AAL1)
    await step_up(session, world.mfa, world.store, "123456", client=CLIENT, now=NOW)
    assert world.mfa.calls[-1] == "verify access-1 app-1"


def test_the_options_never_show_in_logs() -> None:
    challenge = PasskeyChallenge("pk-1", "ch-1", Ceremony.CREATE, OPTIONS)
    assert "publicKey" not in repr(challenge)
    assert "c2VjcmV0" not in repr(challenge)
