"""Passkeys as a second factor, next to the authenticator app (issue #113).

A passkey (Face ID, Touch ID, Windows Hello, an Android fingerprint) is a WebAuthn MFA factor in
Supabase Auth. The browser makes or uses it; Supabase Auth checks it against its relying party
(RP ID and origins are Auth's server config). This module adds what is ours:

- **Step-up for changes.** Adding or removing a second factor needs a current `aal2` session, so
  the first factor is always the authenticator app and a stolen `aal1` session changes nothing.
- **The last-factor guard.** The last verified factor can never be removed, and neither can the
  last authenticator app: it is the recovery when a device with the passkey is lost, and the
  password reset still asks for its code.
- **The origin check.** Before anything reaches Supabase, the credential's `clientDataJSON` must
  name one of the web app's origins; Auth checks it again against its own RP config.
- **Audit.** Enrolling and removing a factor are recorded (the factor's ID and kind, never any
  credential material). The browser's options and its credential pass through as opaque values
  that are never logged (`repr=False`).

Wrong passkeys are throttled per user under the same key as wrong codes.
"""

import base64
import binascii
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from htco_application.audit import AuditRecord
from htco_application.mfa import (
    MAX_CODE_FAILURES,
    Factor,
    MfaProvider,
    mfa_failure_key,
)
from htco_application.web_sessions import (
    ActiveSession,
    ClientInfo,
    CredentialsRefusedError,
    IssuedTokens,
    SessionStore,
    Throttled,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel

_log = logging.getLogger(__name__)

TOTP = "totp"
PASSKEY = "webauthn"


class PasskeysUnavailableError(Exception):
    """Passkeys are switched off on this server (the default until the owner enables them)."""


class StepUpForFactorsError(Exception):
    """Adding or removing a second factor needs a current `aal2` session."""


class LastFactorError(Exception):
    """The factor is the last verified one, or the last authenticator app."""


class FactorNotFoundError(Exception):
    """No such factor of this user (or not a passkey where one is needed)."""


class PasskeyRefusedError(Exception):
    """The passkey was refused: a wrong origin, a malformed credential, or Auth said no."""


class Ceremony(StrEnum):
    """WebAuthn's two ceremonies: `create` makes a passkey, `request` uses one."""

    CREATE = "create"
    REQUEST = "request"


@dataclass(frozen=True)
class PasskeyChallenge:
    factor_id: str
    challenge_id: str
    ceremony: Ceremony
    # The options for `navigator.credentials.create/get`, exactly as Supabase Auth sent them.
    options: Mapping[str, Any] = field(repr=False)


class PasskeyProvider(Protocol):
    async def enroll_passkey(self, access_token: str, *, friendly_name: str) -> str:
        """Creates an unverified passkey factor; returns its ID."""
        ...

    async def challenge_passkey(self, access_token: str, factor_id: str) -> PasskeyChallenge: ...

    async def verify_passkey(
        self,
        access_token: str,
        factor_id: str,
        challenge_id: str,
        ceremony: Ceremony,
        credential: Mapping[str, Any],
    ) -> IssuedTokens:
        """Raises `CredentialsRefusedError` when Auth refuses the credential."""
        ...


class FactorAudit(Protocol):
    async def factor_changed(
        self, user_id: uuid.UUID, *, factor_id: str, kind: str, change: str
    ) -> None:
        """Records `mfa.factor_enrolled` or `mfa.factor_removed` (no credential material)."""
        ...


def _require_aal2(session: ActiveSession) -> None:
    if session.user.assurance is not AssuranceLevel.AAL2:
        raise StepUpForFactorsError("confirm with your second factor first")


def credential_origin(credential: Mapping[str, Any]) -> str | None:
    """The origin the browser wrote into the credential's `clientDataJSON` (base64url)."""
    response = credential.get("response")
    if not isinstance(response, Mapping):
        return None
    encoded = response.get("clientDataJSON")
    if not isinstance(encoded, str) or len(encoded) > 8192:  # noqa: PLR2004 - a sane bound
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        data = json.loads(raw)
    except binascii.Error, ValueError, UnicodeDecodeError:
        return None
    origin = data.get("origin") if isinstance(data, dict) else None
    return origin if isinstance(origin, str) else None


def check_origin(credential: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    """Refuses a credential made for any origin but the web app's (AC-5)."""
    if credential_origin(credential) not in allowed:
        raise PasskeyRefusedError("the passkey was made for another site")


@dataclass(frozen=True)
class Passkeys:
    """The passkey use cases with their collaborators; `provider` None means switched off."""

    mfa: MfaProvider
    provider: PasskeyProvider | None
    store: SessionStore
    audit: FactorAudit
    # The web app's origins; a credential naming any other is refused before Auth sees it.
    origins: tuple[str, ...]

    def _on(self) -> PasskeyProvider:
        if self.provider is None:
            raise PasskeysUnavailableError("passkeys are switched off")
        return self.provider

    @property
    def available(self) -> bool:
        return self.provider is not None

    async def start(self, session: ActiveSession, *, friendly_name: str) -> PasskeyChallenge:
        """Adds an unverified passkey and returns the options to create it in the browser.

        Unfinished passkeys (a creation cancelled in the browser) are removed first, and a name
        already used gets a number, as for authenticator apps."""
        provider = self._on()
        _require_aal2(session)
        access_token = session.tokens.access_token
        kept: set[str | None] = set()
        for factor in await self.mfa.factors_of(access_token):
            if factor.verified:
                kept.add(factor.name)
            elif factor.kind == PASSKEY:
                await self.mfa.remove_factor(access_token, factor.id)
        name, number = friendly_name, 2
        while name in kept:
            name, number = f"{friendly_name} ({number})", number + 1
        factor_id = await provider.enroll_passkey(access_token, friendly_name=name)
        return await provider.challenge_passkey(access_token, factor_id)

    async def finish(  # noqa: PLR0913 - the challenge, the credential and the moment
        self,
        session: ActiveSession,
        factor_id: str,
        challenge_id: str,
        credential: Mapping[str, Any],
        *,
        client: ClientInfo,
        now: datetime,
    ) -> None:
        """Checks the new passkey; it is then a verified second factor (audited)."""
        self._on()
        _require_aal2(session)
        await self._verify(
            session, factor_id, challenge_id, Ceremony.CREATE, credential, client=client, now=now
        )
        await self.audit.factor_changed(
            session.user.user_id, factor_id=factor_id, kind=PASSKEY, change="enrolled"
        )

    async def challenge(self, session: ActiveSession) -> PasskeyChallenge:
        """The options to confirm with a passkey (the sign-in's second step, or a step-up).

        Supabase lists every passkey of the user in one challenge, so any verified passkey
        factor serves; the browser offers whichever this device has."""
        provider = self._on()
        access_token = session.tokens.access_token
        factors = await self.mfa.factors_of(access_token)
        passkey = next((f for f in factors if f.kind == PASSKEY and f.verified), None)
        if passkey is None:
            raise FactorNotFoundError("no passkey is set up")
        return await provider.challenge_passkey(access_token, passkey.id)

    async def confirm(  # noqa: PLR0913 - the challenge, the credential and the moment
        self,
        session: ActiveSession,
        factor_id: str,
        challenge_id: str,
        credential: Mapping[str, Any],
        *,
        client: ClientInfo,
        now: datetime,
    ) -> None:
        """Confirms with a passkey: the session becomes `aal2` and counts as re-authenticated."""
        self._on()
        factors = await self.mfa.factors_of(session.tokens.access_token)
        if not any(f.id == factor_id and f.kind == PASSKEY and f.verified for f in factors):
            raise FactorNotFoundError("no such passkey")
        await self._verify(
            session, factor_id, challenge_id, Ceremony.REQUEST, credential, client=client, now=now
        )

    async def remove(self, session: ActiveSession, factor_id: str) -> Factor:
        """Removes a factor of either kind; never the last verified one or the last app."""
        _require_aal2(session)
        access_token = session.tokens.access_token
        factors = await self.mfa.factors_of(access_token)
        target = next((f for f in factors if f.id == factor_id), None)
        if target is None:
            raise FactorNotFoundError("no such factor")
        if target.verified:
            others = [f for f in factors if f.verified and f.id != factor_id]
            if not others or (target.kind == TOTP and not any(f.kind == TOTP for f in others)):
                raise LastFactorError("keep at least one authenticator app")
        await self.mfa.remove_factor(access_token, factor_id)
        if target.verified:
            await self.audit.factor_changed(
                session.user.user_id, factor_id=factor_id, kind=target.kind, change="removed"
            )
        return target

    async def _verify(  # noqa: PLR0913 - the ceremony, the credential and the moment
        self,
        session: ActiveSession,
        factor_id: str,
        challenge_id: str,
        ceremony: Ceremony,
        credential: Mapping[str, Any],
        *,
        client: ClientInfo,
        now: datetime,
    ) -> None:
        provider = self._on()
        check_origin(credential, self.origins)
        key = mfa_failure_key(session.user.user_id)
        ip_hash = hashlib.sha256(client.ip.encode()).digest() if client.ip else None
        async with Throttled(
            self.store,
            key,
            ip_hash,
            max_by_key=MAX_CODE_FAILURES,
            max_by_ip=None,
            now=now,
            failures=(PasskeyRefusedError,),
        ):
            try:
                issued = await provider.verify_passkey(
                    session.tokens.access_token, factor_id, challenge_id, ceremony, credential
                )
            except CredentialsRefusedError:
                raise PasskeyRefusedError("the passkey was refused") from None
            await self.store.replace_tokens(session.session_id, issued)
            await self.store.set_reauthenticated(session.session_id, now)


class AuditAppender(Protocol):
    async def append(self, record: AuditRecord) -> None: ...


@dataclass(frozen=True)
class FactorAuditLog:
    """Records a factor change once in each organization the user is an active member of: the
    factor's ID and kind only. The change already happened in Supabase Auth, so a failure to
    record it is reported (Sentry, without credential data) rather than undoing the answer."""

    organizations_of: Callable[[uuid.UUID], tuple[uuid.UUID, ...]]
    log: AuditAppender
    offload: Callable[[Callable[[], tuple[uuid.UUID, ...]]], Awaitable[tuple[uuid.UUID, ...]]]

    async def factor_changed(
        self, user_id: uuid.UUID, *, factor_id: str, kind: str, change: str
    ) -> None:
        try:
            organizations = await self.offload(lambda: self.organizations_of(user_id))
            for organization in organizations:
                await self.log.append(
                    AuditRecord.new(
                        organization_id=organization,
                        actor=ActorRef(ActorKind.USER, str(user_id)),
                        action=f"mfa.factor_{change}",
                        entity_type="mfa_factor",
                        entity_id=factor_id,
                        after={"kind": kind},
                    )
                )
        except Exception as error:
            _log.exception("passkeys.audit_failed: %s %s", change, type(error).__name__)
