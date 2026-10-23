"""The Secret Broker and the Authenticator (slice 5.17, ADR-0021, `execution-engine.md` §6).

Contracts and in-memory implementations; the tables (`shop_credentials`, `totp_accounts`,
`totp_events`, `secret_grants`) and the enrolment UI are Phase 8B.

- **One crypto.** Secrets are sealed by the existing vault, `htco_platform.crypto.EnvelopeSealer`
  (AES-256-GCM envelopes on the token vault's key ring), behind the `SecretVault` port, with
  associated data that binds each value to its organization, environment, account and purpose.
  Nothing here encrypts by itself.
- **No model sees a secret.** No tool returns one: `secret_carrying_fields` finds any output
  field that could carry a secret or a code, and the action catalog refuses it (AC-5.17.3). The
  calling actor of an injection receives `injected: true` only. `SecretScanner` remembers what
  was released (as SHA-256 fingerprints, never the values) and blocks any model input or log
  line that contains it.
- **The broker** issues a single-use grant bound to one execution, one environment and one
  field, for at most 60 s; only a browser worker redeems it; every grant, redemption and refusal
  is audited without the value (AC-5.17.2).
- **The authenticator** generates codes on the server only, through the `TotpCodes` port (PyOTP
  in `htco_integrations.totp`). A code is injected at most once per period (replay protection),
  at most 3 injections per 10 minutes per environment, and shown at most once per period and 10
  times an hour per user, to the Owner and Admins with a second factor and a recent
  re-authentication. Every enrolment, rotation, removal, view, injection and refusal is audited.
"""

import hashlib
import logging
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, override

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import STEP_UP_WINDOW
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import ConsentRecord
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.secrets import (
    MASK,
    MAX_GRANT_TTL,
    DisplayedCode,
    InjectionGrant,
    SecretKind,
    SecretRef,
    SecretValue,
    period_window,
)

BROWSER_WORKER_PREFIX = "browser-worker"
INJECTIONS_PER_WINDOW = 3
INJECTION_WINDOW = timedelta(minutes=10)
VIEWS_PER_HOUR = 10


# --- The static rule: no tool output can carry a secret (AC-5.17.3) ------------------------------

_SECRET_NAME = re.compile(
    r"(^|_)(password|passwd|passphrase|secret|totp|otp|hotp|passcode|one_time_code|mfa_code|"
    r"otp_code|verification_code|api_key|access_token|refresh_token|session_token|cookie|"
    r"otpauth)($|_)"
)


def secret_carrying_fields(schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Every place in a JSON Schema that could carry a secret or a one-time code: a property
    named like one, a `password` format, or a `writeOnly` field."""
    found: list[str] = []
    if schema.get("format") == "password" or schema.get("writeOnly") is True:
        found.append(path)
    for name, child in dict(schema.get("properties", {})).items():
        here = f"{path}.{name}"
        if _SECRET_NAME.search(str(name).lower()):
            found.append(here)
        if isinstance(child, Mapping):
            found.extend(secret_carrying_fields(child, here))
    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, Mapping):
            found.extend(secret_carrying_fields(child, f"{path}[]"))
    return found


def secret_tool_names(names: Iterable[str]) -> list[str]:
    """Tools whose name says they hand out a secret or a code: none may exist (ADR-0021)."""
    return sorted(
        n for n in names if any(_SECRET_NAME.search(part) for part in n.lower().split("."))
    )


# --- Fingerprints: blocking model inputs and log lines ------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-.!@#$%^&*]{4,}")


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SecretInModelInputError(Exception):
    """A model call whose input contains a secret or a code: blocked before it is sent."""


@dataclass
class SecretScanner:
    """Remembers SHA-256 fingerprints of released secrets and codes until they lapse."""

    _active: dict[str, datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def remember(self, value: SecretValue, *, until: datetime) -> None:
        with self._lock:
            key = _fingerprint(value.reveal())
            self._active[key] = max(until, self._active.get(key, until))

    def leaks(self, text: str, *, now: datetime) -> bool:
        if "otpauth:" in text.lower():
            return True
        with self._lock:
            for key, until in list(self._active.items()):
                if until <= now:
                    del self._active[key]
            if not self._active:
                return False
            return any(_fingerprint(token) in self._active for token in _candidates(text))

    def guard_model_input(self, texts: Sequence[str], *, now: datetime) -> None:
        if any(self.leaks(text, now=now) for text in texts):
            raise SecretInModelInputError("a model input contains a secret or a one-time code")


def _candidates(text: str) -> Iterable[str]:
    for token in _TOKEN.findall(text):
        yield token
        yield token.strip(".,;:!?\"'()[]{}")
        for part in re.split(r"[=:/?&]", token):  # "secret=...", "key:value"
            yield part.strip(".,;!\"'()[]{}")


class SecretLogFilter(logging.Filter):
    """Masks any log record whose message or arguments contain a remembered secret or code."""

    def __init__(self, scanner: SecretScanner, clock: Callable[[], datetime]) -> None:
        super().__init__()
        self._scanner = scanner
        self._clock = clock

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if self._scanner.leaks(record.getMessage(), now=self._clock()):
            record.msg, record.args = f"{MASK} (a log line with a secret was masked)", ()
        return True


# --- Ports --------------------------------------------------------------------------------------


class SecretVault(Protocol):
    """The existing vault (`EnvelopeSealer`): AES-256-GCM envelopes on the token key ring."""

    def seal(self, plaintexts: Sequence[str], *, context: bytes) -> Any: ...

    def open(self, envelope: Any, *, context: bytes) -> tuple[str, ...]: ...


class SecretAuditAction(StrEnum):
    STORED = "stored"
    GRANTED = "granted"
    REDEEMED = "redeemed"
    ENROLLED = "enrolled"
    ROTATED = "rotated"
    REMOVED = "removed"
    VIEWED = "viewed"
    INJECTED = "injected"
    REFUSED = "refused"


@dataclass(frozen=True)
class SecretAuditEvent:
    """Who, what, which execution and the outcome; never a value."""

    action: SecretAuditAction
    actor: ActorRef
    organization_id: uuid.UUID
    at: datetime
    environment_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    execution_id: uuid.UUID | None = None
    kind: SecretKind | None = None
    reason: str | None = None  # a code: "expired", "replayed", "rate_limited", ...


class SecretAudit(Protocol):
    def record(self, event: SecretAuditEvent) -> None: ...


@dataclass
class MemorySecretAudit:
    events: list[SecretAuditEvent] = field(default_factory=list)

    def record(self, event: SecretAuditEvent) -> None:
        self.events.append(event)


class SecretRefusedError(Exception):
    def __init__(self, code: str, reason: str, *, retry_at: datetime | None = None) -> None:
        super().__init__(reason)
        self.code = code
        self.retry_at = retry_at


def _context(organization_id: uuid.UUID, ref: SecretRef) -> bytes:
    account = "" if ref.account_id is None else str(ref.account_id)
    return f"htco:secret:{organization_id}:{ref.environment_id}:{account}:{ref.kind}".encode()


# --- The Secret Broker --------------------------------------------------------------------------


class SecretBroker(Protocol):  # contract in Phase 5, real in 8B
    def issue_injection_grant(  # noqa: PLR0913 - the grant's bindings
        self,
        *,
        execution_id: uuid.UUID,
        environment_id: uuid.UUID,
        secret_ref: SecretRef,
        field: str,
        ttl: timedelta,
        by: ActorRef,
    ) -> InjectionGrant: ...

    def redeem(self, grant: InjectionGrant, *, worker: ActorRef) -> SecretValue: ...


class CodeSource(Protocol):
    """Where the broker gets a TOTP code for a grant (the authenticator)."""

    def code_for_injection(self, grant: InjectionGrant) -> SecretValue: ...


@dataclass(frozen=True)
class _Stored:
    organization_id: uuid.UUID
    envelope: Any = field(repr=False)


class MemorySecretBroker:
    """Passwords sealed by the vault, single-use grants, and every access audited."""

    def __init__(  # noqa: PLR0913 - the broker's collaborators, all injected
        self,
        vault: SecretVault,
        audit: SecretAudit,
        scanner: SecretScanner,
        clock: Callable[[], datetime],
        *,
        organization_of: Callable[[uuid.UUID], uuid.UUID | None],
        codes: CodeSource | None = None,
    ) -> None:
        self._vault = vault
        self._audit = audit
        self._scanner = scanner
        self._clock = clock
        self._organization_of = organization_of  # environment -> organization
        self._codes = codes
        self._secrets: dict[SecretRef, _Stored] = {}
        self._grants: dict[uuid.UUID, InjectionGrant] = {}
        self._redeemed: set[uuid.UUID] = set()
        self._lock = threading.Lock()

    def attach_codes(self, codes: CodeSource) -> None:
        self._codes = codes

    def store(self, ref: SecretRef, value: SecretValue, *, by: ActorRef) -> None:
        """Seal a password (Phase 8B's credential form); TOTP secrets go to the authenticator."""
        if ref.kind is SecretKind.TOTP:
            raise ValueError("TOTP secrets are enrolled through the authenticator")
        organization = self._organization(ref.environment_id)
        envelope = self._vault.seal([value.reveal()], context=_context(organization, ref))
        with self._lock:
            self._secrets[ref] = _Stored(organization, envelope)
        self._log(SecretAuditAction.STORED, by, organization, ref)

    def issue_injection_grant(  # noqa: PLR0913 - the grant's bindings
        self,
        *,
        execution_id: uuid.UUID,
        environment_id: uuid.UUID,
        secret_ref: SecretRef,
        field: str,
        ttl: timedelta,
        by: ActorRef,
    ) -> InjectionGrant:
        """For the execution's own environment; an automation or employee asks for its run."""
        if by.kind not in {ActorKind.AUTOMATION, ActorKind.EMPLOYEE, ActorKind.SYSTEM}:
            raise SecretRefusedError("forbidden", "only a run asks for an injection")
        if ttl > MAX_GRANT_TTL:
            raise SecretRefusedError("ttl_too_long", "a grant lives for at most 60 seconds")
        now = self._clock()
        grant = InjectionGrant(
            grant_id=uuid.uuid7(), execution_id=execution_id, environment_id=environment_id,
            secret_ref=secret_ref, field=field, issued_at=now, expires_at=now + ttl,
        )  # fmt: skip
        organization = self._organization(environment_id)
        with self._lock:
            self._grants[grant.grant_id] = grant
        self._log(SecretAuditAction.GRANTED, by, organization, secret_ref, execution=execution_id)
        return grant

    def redeem(self, grant: InjectionGrant, *, worker: ActorRef) -> SecretValue:
        """Browser workers only; one use; before it expires; audited."""
        organization = self._organization(grant.environment_id)
        try:
            value = self._redeem(grant, worker)
        except SecretRefusedError as refusal:
            self._log(SecretAuditAction.REFUSED, worker, organization, grant.secret_ref,
                      execution=grant.execution_id, reason=refusal.code)  # fmt: skip
            raise
        self._scanner.remember(value, until=grant.expires_at + timedelta(minutes=5))
        self._log(SecretAuditAction.REDEEMED, worker, organization, grant.secret_ref,
                  execution=grant.execution_id)  # fmt: skip
        return value

    def _redeem(self, grant: InjectionGrant, worker: ActorRef) -> SecretValue:
        if worker.kind is not ActorKind.SYSTEM or not worker.id.startswith(BROWSER_WORKER_PREFIX):
            raise SecretRefusedError("forbidden", "only a browser worker redeems a grant")
        with self._lock:
            issued = self._grants.get(grant.grant_id)
            if issued is None or issued != grant:
                raise SecretRefusedError("unknown_grant", "no such grant")
            if grant.grant_id in self._redeemed:
                raise SecretRefusedError("used", "a grant is used once")
            if grant.expired(self._clock()):
                raise SecretRefusedError("expired", "the grant expired")
            self._redeemed.add(grant.grant_id)
            stored = self._secrets.get(grant.secret_ref)
        if grant.secret_ref.kind is SecretKind.TOTP:
            if self._codes is None:
                raise SecretRefusedError("no_authenticator", "no authenticator is attached")
            return self._codes.code_for_injection(grant)
        if stored is None:
            raise SecretRefusedError("not_stored", "no such secret")
        [value] = self._vault.open(
            stored.envelope, context=_context(stored.organization_id, grant.secret_ref)
        )
        return SecretValue(value)

    def _organization(self, environment_id: uuid.UUID) -> uuid.UUID:
        organization = self._organization_of(environment_id)
        if organization is None:
            raise SecretRefusedError("unknown_environment", "no such environment")
        return organization

    def _log(  # noqa: PLR0913 - one argument per audited column
        self,
        action: SecretAuditAction,
        actor: ActorRef,
        organization: uuid.UUID,
        ref: SecretRef,
        *,
        execution: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        self._audit.record(
            SecretAuditEvent(
                action=action, actor=actor, organization_id=organization, at=self._clock(),
                environment_id=ref.environment_id, account_id=ref.account_id,
                execution_id=execution, kind=ref.kind, reason=reason,
            )
        )  # fmt: skip


# --- The Authenticator --------------------------------------------------------------------------


@dataclass(frozen=True)
class TotpParameters:
    secret: SecretValue
    digits: int = 6
    period_seconds: int = 30


class TotpCodes(Protocol):
    """RFC 6238 on the server (PyOTP in `htco_integrations.totp`)."""

    def parse(self, secret_or_uri: SecretValue) -> TotpParameters: ...

    def code(self, parameters: TotpParameters, *, at: datetime) -> SecretValue: ...

    def verify(self, parameters: TotpParameters, code: str, *, at: datetime) -> bool: ...


class TotpStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


@dataclass(frozen=True)
class TotpAccount:
    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    environment_id: uuid.UUID
    label: str
    digits: int
    period_seconds: int
    status: TotpStatus
    consent: ConsentRecord
    envelope: Any = field(repr=False, compare=False)

    @property
    def ref(self) -> SecretRef:
        return SecretRef(SecretKind.TOTP, self.environment_id, self.id)


class Authenticator(Protocol):  # contract in Phase 5, real in 8B
    def enrol(  # noqa: PLR0913 - the account and its consent
        self,
        *,
        shop_id: uuid.UUID,
        environment_id: uuid.UUID,
        label: str,
        secret_or_uri: SecretValue,
        confirm_code: str,
        by: AuthContext,
        consent: ConsentRecord,
    ) -> TotpAccount: ...

    def rotate(
        self, account_id: uuid.UUID, *, new_secret: SecretValue, confirm_code: str, by: AuthContext
    ) -> TotpAccount: ...

    def remove(self, account_id: uuid.UUID, *, by: AuthContext) -> None: ...

    def current_code_for_display(
        self, account_id: uuid.UUID, *, viewer: AuthContext
    ) -> DisplayedCode: ...

    def code_for_injection(self, grant: InjectionGrant) -> SecretValue: ...


_MANAGERS = frozenset({Role.OWNER, Role.ADMIN})


class MemoryAuthenticator:
    """TOTP accounts sealed by the vault, codes generated on the server only."""

    def __init__(  # noqa: PLR0913 - the authenticator's collaborators, all injected
        self,
        codes: TotpCodes,
        vault: SecretVault,
        audit: SecretAudit,
        scanner: SecretScanner,
        clock: Callable[[], datetime],
        *,
        organization_of_shop: Callable[[uuid.UUID], uuid.UUID | None],
    ) -> None:
        self._codes = codes
        self._vault = vault
        self._audit = audit
        self._scanner = scanner
        self._clock = clock
        self._organization_of_shop = organization_of_shop
        self.accounts: dict[uuid.UUID, TotpAccount] = {}
        self._injected_counter: dict[uuid.UUID, int] = {}
        self._injections: dict[uuid.UUID, deque[datetime]] = {}
        self._views: dict[tuple[uuid.UUID, uuid.UUID], deque[tuple[datetime, int]]] = {}
        self._lock = threading.Lock()

    # --- Owner and Admin --------------------------------------------------------------------

    def enrol(  # noqa: PLR0913 - the account and its consent
        self,
        *,
        shop_id: uuid.UUID,
        environment_id: uuid.UUID,
        label: str,
        secret_or_uri: SecretValue,
        confirm_code: str,
        by: AuthContext,
        consent: ConsentRecord,
    ) -> TotpAccount:
        """The first code is confirmed before the secret is saved."""
        organization = self._organization_of_shop(shop_id)
        if organization is None:
            raise SecretRefusedError("not_found", "no such shop")
        self._manager(by, organization, step_up=True)
        if consent.user_id != by.user_id:
            raise SecretRefusedError("consent_required", "the enrolling user gives the consent")
        if not label.strip() or len(label) > 100:  # noqa: PLR2004 - the label's column size
            raise ValueError("a TOTP account needs a label of 1 to 100 characters")
        parameters = self._confirmed(secret_or_uri, confirm_code, by, organization)
        account_id = uuid.uuid7()
        ref = SecretRef(SecretKind.TOTP, environment_id, account_id)
        account = TotpAccount(
            id=account_id, organization_id=organization, shop_id=shop_id,
            environment_id=environment_id, label=label, digits=parameters.digits,
            period_seconds=parameters.period_seconds, status=TotpStatus.ACTIVE, consent=consent,
            envelope=self._seal(organization, ref, parameters.secret),
        )  # fmt: skip
        with self._lock:
            self.accounts[account_id] = account
        self._log(SecretAuditAction.ENROLLED, by, account)
        return account

    def rotate(
        self, account_id: uuid.UUID, *, new_secret: SecretValue, confirm_code: str, by: AuthContext
    ) -> TotpAccount:
        """The new secret replaces the old one only after its first code is confirmed."""
        account = self._active(account_id)
        self._manager(by, account.organization_id, step_up=True)
        parameters = self._confirmed(new_secret, confirm_code, by, account.organization_id)
        rotated = TotpAccount(
            id=account.id, organization_id=account.organization_id, shop_id=account.shop_id,
            environment_id=account.environment_id, label=account.label,
            digits=parameters.digits, period_seconds=parameters.period_seconds,
            status=TotpStatus.ACTIVE, consent=account.consent,
            envelope=self._seal(account.organization_id, account.ref, parameters.secret),
        )  # fmt: skip
        with self._lock:
            self.accounts[account_id] = rotated
        self._log(SecretAuditAction.ROTATED, by, rotated)
        return rotated

    def remove(self, account_id: uuid.UUID, *, by: AuthContext) -> None:
        """The ciphertext is destroyed; the account stays as REMOVED for the audit trail."""
        account = self._active(account_id)
        self._manager(by, account.organization_id, step_up=True)
        removed = TotpAccount(
            id=account.id, organization_id=account.organization_id, shop_id=account.shop_id,
            environment_id=account.environment_id, label=account.label, digits=account.digits,
            period_seconds=account.period_seconds, status=TotpStatus.REMOVED,
            consent=account.consent, envelope=None,
        )  # fmt: skip
        with self._lock:
            self.accounts[account_id] = removed
        self._log(SecretAuditAction.REMOVED, by, removed)

    def current_code_for_display(
        self, account_id: uuid.UUID, *, viewer: AuthContext
    ) -> DisplayedCode:
        """At most one view per period and 10 an hour per user; audited, never the code."""
        account = self._active(account_id)
        try:
            self._manager(viewer, account.organization_id, step_up=True)
        except SecretRefusedError as refusal:
            self._log(SecretAuditAction.REFUSED, viewer, account, reason=refusal.code)
            raise
        now = self._clock()
        counter, ends = period_window(now, account.period_seconds)
        with self._lock:
            views = self._views.setdefault((account_id, viewer.user_id), deque())
            while views and views[0][0] <= now - timedelta(hours=1):
                views.popleft()
            if any(c == counter for _, c in views) or len(views) >= VIEWS_PER_HOUR:
                refused = SecretRefusedError("rate_limited", "too many code views", retry_at=ends)
            else:
                refused = None
                views.append((now, counter))
        if refused is not None:
            self._log(SecretAuditAction.REFUSED, viewer, account, reason=refused.code)
            raise refused
        code = self._code(account, now)
        self._log(SecretAuditAction.VIEWED, viewer, account)
        return DisplayedCode(code, ends, account.period_seconds)

    # --- The broker -------------------------------------------------------------------------

    def code_for_injection(self, grant: InjectionGrant) -> SecretValue:
        """Called by the broker for a valid grant. Once per period per account; at most 3 per
        10 minutes per environment."""
        if grant.secret_ref.account_id is None:
            raise SecretRefusedError("not_totp", "the grant is not for a TOTP account")
        account = self._active(grant.secret_ref.account_id)
        worker = ActorRef(ActorKind.SYSTEM, BROWSER_WORKER_PREFIX)
        if account.environment_id != grant.environment_id:
            raise SecretRefusedError("forbidden", "the account belongs to another environment")
        now = self._clock()
        counter, ends = period_window(now, account.period_seconds)
        with self._lock:
            recent = self._injections.setdefault(account.environment_id, deque())
            while recent and recent[0] <= now - INJECTION_WINDOW:
                recent.popleft()
            refused = None
            if self._injected_counter.get(account.id) == counter:
                refused = SecretRefusedError("replayed", "one code per period", retry_at=ends)
            elif len(recent) >= INJECTIONS_PER_WINDOW:
                refused = SecretRefusedError(
                    "rate_limited", "too many injections", retry_at=recent[0] + INJECTION_WINDOW
                )
            else:
                self._injected_counter[account.id] = counter
                recent.append(now)
        if refused is not None:
            self._log(SecretAuditAction.REFUSED, worker, account, execution=grant.execution_id,
                      reason=refused.code)  # fmt: skip
            raise refused
        code = self._code(account, now)
        self._log(SecretAuditAction.INJECTED, worker, account, execution=grant.execution_id)
        return code

    # --- Inside -----------------------------------------------------------------------------

    def _manager(self, by: AuthContext, organization: uuid.UUID, *, step_up: bool) -> None:
        try:
            principal = by.principal_for(organization)
        except NoMembershipError:
            raise SecretRefusedError("not_found", "no such account") from None
        if principal.role not in _MANAGERS:
            raise SecretRefusedError("forbidden", "only the Owner and Admins")
        if principal.assurance is not AssuranceLevel.AAL2:
            raise SecretRefusedError("mfa_required", "a second factor is required")
        at, now = principal.reauthenticated_at, self._clock()
        if step_up and not (at is not None and now - STEP_UP_WINDOW < at <= now):
            raise SecretRefusedError("step_up_required", "a re-authentication is required")

    def _confirmed(
        self, secret: SecretValue, confirm_code: str, by: AuthContext, organization: uuid.UUID
    ) -> TotpParameters:
        parameters = self._codes.parse(secret)
        self._scanner.remember(parameters.secret, until=datetime.max.replace(tzinfo=UTC))
        if not self._codes.verify(parameters, confirm_code, at=self._clock()):
            self._audit.record(
                SecretAuditEvent(SecretAuditAction.REFUSED, _actor(by), organization,
                                 self._clock(), reason="confirm_code_wrong")
            )  # fmt: skip
            raise SecretRefusedError("confirm_code_wrong", "the code does not match the secret")
        return parameters

    def _seal(self, organization: uuid.UUID, ref: SecretRef, secret: SecretValue) -> Any:
        return self._vault.seal([secret.reveal()], context=_context(organization, ref))

    def _code(self, account: TotpAccount, now: datetime) -> SecretValue:
        [secret] = self._vault.open(
            account.envelope, context=_context(account.organization_id, account.ref)
        )
        parameters = TotpParameters(SecretValue(secret), account.digits, account.period_seconds)
        code = self._codes.code(parameters, at=now)
        _, ends = period_window(now, account.period_seconds)
        self._scanner.remember(code, until=ends + timedelta(seconds=account.period_seconds))
        return code

    def _active(self, account_id: uuid.UUID) -> TotpAccount:
        with self._lock:
            account = self.accounts.get(account_id)
        if account is None or account.status is not TotpStatus.ACTIVE:
            raise SecretRefusedError("not_found", "no such TOTP account")
        return account

    def _log(
        self,
        action: SecretAuditAction,
        by: AuthContext | ActorRef,
        account: TotpAccount,
        *,
        execution: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        self._audit.record(
            SecretAuditEvent(
                action=action, actor=by if isinstance(by, ActorRef) else _actor(by),
                organization_id=account.organization_id, at=self._clock(),
                environment_id=account.environment_id, account_id=account.id,
                execution_id=execution, kind=SecretKind.TOTP, reason=reason,
            )
        )  # fmt: skip


def _actor(auth: AuthContext) -> ActorRef:
    return ActorRef(ActorKind.USER, str(auth.user_id))
