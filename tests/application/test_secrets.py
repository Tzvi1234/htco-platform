"""The Secret Broker and the Authenticator (slice 5.17): grants, the vault, replay protection,
rate limits, the audit, the scanner and the registry rule. The vault is the real
`EnvelopeSealer`; TOTP codes come from a deterministic fake of the `TotpCodes` port (PyOTP is
tested in tests/integrations/test_totp.py)."""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.execution.contracts import ActionCatalogError
from htco_application.secrets import (
    MemoryAuthenticator,
    MemorySecretAudit,
    MemorySecretBroker,
    SecretAuditAction,
    SecretInModelInputError,
    SecretLogFilter,
    SecretRefusedError,
    SecretScanner,
    TotpParameters,
    secret_carrying_fields,
    secret_tool_names,
)
from htco_application.workforce.files import read_registry
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import ConsentRecord
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.secrets import SecretKind, SecretRef, SecretValue, period_window
from htco_platform.crypto import EnvelopeSealer, KeyRing
from tests.execution_fakes import REGISTRY, SCHEMAS, real_catalog
from tests.log_capture import capture_logs

NOW = datetime(2026, 10, 24, 10, 0, 5, tzinfo=UTC)
ORG = uuid.UUID("8a000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("8b000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("8a300000-0000-0000-0000-000000000001")
ENV = uuid.UUID("8a400000-0000-0000-0000-000000000001")
OWNER = uuid.UUID("8a100000-0000-0000-0000-000000000001")
PASSWORD = "Fake-Etsy-Password-7Q"  # noqa: S105 - a fake to search for
TOTP_SECRET = "JBSWY3DPEHPK3PXPFAKE"  # noqa: S105 - a fake base32 secret
WORKER = ActorRef(ActorKind.SYSTEM, "browser-worker:b1")
RUN = ActorRef(ActorKind.AUTOMATION, "etsy-order-intake")


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeCodes:
    """Deterministic codes from the secret and the period counter (not RFC 6238)."""

    def parse(self, secret_or_uri: SecretValue) -> TotpParameters:
        return TotpParameters(secret_or_uri)

    def code(self, parameters: TotpParameters, *, at: datetime) -> SecretValue:
        counter, _ = period_window(at, parameters.period_seconds)
        digest = hashlib.sha256(f"{parameters.secret.reveal()}:{counter}".encode()).hexdigest()
        return SecretValue(str(int(digest, 16) % 10**6).zfill(6))

    def verify(self, parameters: TotpParameters, code: str, *, at: datetime) -> bool:
        return self.code(parameters, at=at).reveal() == code


def owner(
    *,
    role: Role = Role.OWNER,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    reauthenticated_at: datetime | None = NOW,
    user: uuid.UUID = OWNER,
) -> AuthContext:
    return AuthContext(
        user_id=user, assurance=assurance, session_id=None,
        memberships=(Membership(ORG, role, frozenset({SHOP})),),
        reauthenticated_at=reauthenticated_at,
    )  # fmt: skip


def consent(user: uuid.UUID = OWNER) -> ConsentRecord:
    return ConsentRecord(text_version="totp-v1", user_id=user, at=NOW, ip_hash="e" * 64)


@dataclass
class Vault:
    """The real vault, counting its calls."""

    sealer: EnvelopeSealer = field(
        default_factory=lambda: EnvelopeSealer(KeyRing(("key-a", os.urandom(32))))
    )
    sealed: list[Any] = field(default_factory=list)

    def seal(self, plaintexts: Any, *, context: bytes) -> Any:
        envelope = self.sealer.seal(plaintexts, context=context)
        self.sealed.append(envelope)
        return envelope

    def open(self, envelope: Any, *, context: bytes) -> tuple[str, ...]:
        return self.sealer.open(envelope, context=context)


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.audit = MemorySecretAudit()
        self.scanner = SecretScanner()
        self.vault = Vault()
        self.codes = FakeCodes()
        self.authenticator = MemoryAuthenticator(
            self.codes, self.vault, self.audit, self.scanner, self.clock,
            organization_of_shop={SHOP: ORG}.get,
        )  # fmt: skip
        self.broker = MemorySecretBroker(
            self.vault, self.audit, self.scanner, self.clock, organization_of={ENV: ORG}.get,
            codes=self.authenticator,
        )  # fmt: skip

    def store_password(self) -> SecretRef:
        ref = SecretRef(SecretKind.ETSY_PASSWORD, ENV)
        self.broker.store(ref, SecretValue(PASSWORD), by=ActorRef(ActorKind.USER, str(OWNER)))
        return ref

    def grant(self, ref: SecretRef, *, ttl: int = 60, execution: uuid.UUID | None = None) -> Any:
        return self.broker.issue_injection_grant(
            execution_id=execution or uuid.uuid4(), environment_id=ENV, secret_ref=ref,
            field="password", ttl=timedelta(seconds=ttl), by=RUN,
        )  # fmt: skip

    def enrol(self) -> Any:
        secret = SecretValue(TOTP_SECRET)
        code = self.codes.code(TotpParameters(secret), at=self.clock()).reveal()
        return self.authenticator.enrol(
            shop_id=SHOP, environment_id=ENV, label="Etsy", secret_or_uri=secret,
            confirm_code=code, by=owner(), consent=consent(),
        )  # fmt: skip

    def actions(self) -> list[SecretAuditAction]:
        return [e.action for e in self.audit.events]


# --- The broker (AC-5.17.2) ----------------------------------------------------------------------


@pytest.mark.ac("AC-5.17.2")
def test_grant__redeemed_once_by_a_browser_worker() -> None:
    world = World()
    grant = world.grant(world.store_password())

    value = world.broker.redeem(grant, worker=WORKER)
    with pytest.raises(SecretRefusedError) as again:
        world.broker.redeem(grant, worker=WORKER)

    assert value.reveal() == PASSWORD
    assert again.value.code == "used"


@pytest.mark.ac("AC-5.17.2")
def test_grant__expires_within_60_seconds() -> None:
    world = World()
    grant = world.grant(world.store_password())
    world.clock.now = NOW + timedelta(seconds=60)

    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(grant, worker=WORKER)

    assert refused.value.code == "expired"
    with pytest.raises(SecretRefusedError) as long:
        world.grant(world.store_password(), ttl=61)
    assert long.value.code == "ttl_too_long"


@pytest.mark.ac("AC-5.17.2")
def test_grant__bound_to_its_execution_and_environment() -> None:
    world = World()
    grant = world.grant(world.store_password())

    forged = replace(grant, execution_id=uuid.uuid4())

    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(forged, worker=WORKER)
    assert refused.value.code == "unknown_grant"
    assert world.broker.redeem(grant, worker=WORKER).reveal() == PASSWORD


@pytest.mark.ac("AC-5.17.2")
@pytest.mark.parametrize(
    "worker",
    [
        ActorRef(ActorKind.AUTOMATION, "etsy-order-intake"),
        ActorRef(ActorKind.EMPLOYEE, "eitan"),
        ActorRef(ActorKind.SYSTEM, "agent-worker:w1"),
        ActorRef(ActorKind.USER, str(OWNER)),
    ],
)
def test_redeem__anyone_but_a_browser_worker__refused(worker: ActorRef) -> None:
    world = World()
    grant = world.grant(world.store_password())

    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(grant, worker=worker)

    assert refused.value.code == "forbidden"
    assert world.broker.redeem(grant, worker=WORKER).reveal() == PASSWORD  # still unused


def test_grant__a_user_or_advisor_cannot_ask_for_one() -> None:
    world = World()
    ref = world.store_password()

    for by in (ActorRef(ActorKind.USER, str(OWNER)), ActorRef(ActorKind.ADVISOR, "jev")):
        with pytest.raises(SecretRefusedError):
            world.broker.issue_injection_grant(
                execution_id=uuid.uuid4(), environment_id=ENV, secret_ref=ref, field="password",
                ttl=timedelta(seconds=30), by=by,
            )  # fmt: skip


def test_broker__every_access_audited_without_the_value() -> None:
    world = World()
    grant = world.grant(world.store_password())
    world.broker.redeem(grant, worker=WORKER)
    with pytest.raises(SecretRefusedError):
        world.broker.redeem(grant, worker=WORKER)

    assert world.actions() == [
        SecretAuditAction.STORED,
        SecretAuditAction.GRANTED,
        SecretAuditAction.REDEEMED,
        SecretAuditAction.REFUSED,
    ]
    assert world.audit.events[2].execution_id == grant.execution_id
    assert PASSWORD not in repr(world.audit.events)


def test_broker__secrets_go_through_the_existing_vault() -> None:
    world = World()
    ref = world.store_password()

    [envelope] = world.vault.sealed

    assert PASSWORD.encode() not in b"".join(envelope.ciphertexts)
    assert envelope.key_id == "key-a"
    with pytest.raises(ValueError, match="authenticator"):
        world.broker.store(replace(ref, kind=SecretKind.TOTP, account_id=uuid.uuid4()),
                           SecretValue("x"), by=RUN)  # fmt: skip


def test_broker__unknown_environment__refused() -> None:
    world = World()

    unknown = uuid.uuid4()
    with pytest.raises(SecretRefusedError):
        world.broker.issue_injection_grant(
            execution_id=uuid.uuid4(), environment_id=unknown,
            secret_ref=SecretRef(SecretKind.ETSY_PASSWORD, unknown), field="password",
            ttl=timedelta(seconds=10), by=RUN,
        )  # fmt: skip


def test_redeem__a_password_never_stored__refused() -> None:
    world = World()
    grant = world.grant(SecretRef(SecretKind.PARTNER_PASSWORD, ENV))

    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(grant, worker=WORKER)

    assert refused.value.code == "not_stored"


# --- The authenticator ---------------------------------------------------------------------------


def test_enrol__confirmed_first_sealed_and_audited() -> None:
    world = World()

    account = world.enrol()

    assert (account.organization_id, account.label, account.status.value) == (ORG, "Etsy", "active")
    assert TOTP_SECRET not in repr(account)
    assert world.actions() == [SecretAuditAction.ENROLLED]


def test_enrol__a_wrong_first_code__nothing_saved() -> None:
    world = World()

    with pytest.raises(SecretRefusedError) as refused:
        world.authenticator.enrol(
            shop_id=SHOP, environment_id=ENV, label="Etsy", secret_or_uri=SecretValue(TOTP_SECRET),
            confirm_code="000000", by=owner(), consent=consent(),
        )  # fmt: skip

    assert refused.value.code == "confirm_code_wrong"
    assert world.authenticator.accounts == {}
    assert world.vault.sealed == []


@pytest.mark.parametrize(
    ("by", "code"),
    [
        (owner(role=Role.OPERATIONS), "forbidden"),
        (owner(assurance=AssuranceLevel.AAL1), "mfa_required"),
        (owner(reauthenticated_at=None), "step_up_required"),
    ],
)
def test_enrol__owner_or_admin_with_mfa_and_step_up(by: AuthContext, code: str) -> None:
    world = World()

    with pytest.raises(SecretRefusedError) as refused:
        world.authenticator.enrol(
            shop_id=SHOP, environment_id=ENV, label="Etsy", secret_or_uri=SecretValue(TOTP_SECRET),
            confirm_code="000000", by=by, consent=consent(),
        )  # fmt: skip

    assert refused.value.code == code


def test_enrol__the_consent_is_the_enrolling_users() -> None:
    world = World()

    with pytest.raises(SecretRefusedError) as refused:
        world.authenticator.enrol(
            shop_id=SHOP, environment_id=ENV, label="Etsy", secret_or_uri=SecretValue(TOTP_SECRET),
            confirm_code="000000", by=owner(), consent=consent(uuid.uuid4()),
        )  # fmt: skip

    assert refused.value.code == "consent_required"


def test_display__the_code_once_per_period_and_audited() -> None:
    world = World()
    account = world.enrol()

    shown = world.authenticator.current_code_for_display(account.id, viewer=owner())
    with pytest.raises(SecretRefusedError) as again:
        world.authenticator.current_code_for_display(account.id, viewer=owner())

    assert shown.period_seconds == 30
    assert shown.period_ends_at == datetime(2026, 10, 24, 10, 0, 30, tzinfo=UTC)
    assert again.value.code == "rate_limited"
    assert again.value.retry_at == shown.period_ends_at
    assert world.actions()[-2:] == [SecretAuditAction.VIEWED, SecretAuditAction.REFUSED]
    assert shown.code.reveal() not in repr(world.audit.events)


def test_display__at_most_ten_an_hour() -> None:
    world = World()
    account = world.enrol()
    for period in range(10):
        world.clock.now = NOW + timedelta(seconds=30 * period)
        world.authenticator.current_code_for_display(account.id, viewer=owner(
            reauthenticated_at=world.clock.now))  # fmt: skip

    world.clock.now = NOW + timedelta(seconds=300)
    with pytest.raises(SecretRefusedError) as refused:
        world.authenticator.current_code_for_display(
            account.id, viewer=owner(reauthenticated_at=world.clock.now)
        )

    assert refused.value.code == "rate_limited"


def test_display__other_roles_refused_and_audited() -> None:
    world = World()
    account = world.enrol()

    with pytest.raises(SecretRefusedError):
        world.authenticator.current_code_for_display(account.id, viewer=owner(role=Role.VIEWER))

    assert world.actions()[-1] is SecretAuditAction.REFUSED


def test_injection__once_per_period_replay_refused() -> None:
    world = World()
    account = world.enrol()
    ref = account.ref

    first = world.broker.redeem(world.grant(ref), worker=WORKER)
    with pytest.raises(SecretRefusedError) as replay:
        world.broker.redeem(world.grant(ref), worker=WORKER)
    world.clock.now = NOW + timedelta(seconds=30)
    next_period = world.broker.redeem(world.grant(ref), worker=WORKER)

    assert len(first.reveal()) == 6
    assert replay.value.code == "replayed"
    assert replay.value.retry_at == datetime(2026, 10, 24, 10, 0, 30, tzinfo=UTC)
    assert next_period != first


def test_injection__at_most_three_per_ten_minutes_per_environment() -> None:
    world = World()
    ref = world.enrol().ref
    for period in range(3):
        world.clock.now = NOW + timedelta(seconds=30 * period)
        world.broker.redeem(world.grant(ref), worker=WORKER)

    world.clock.now = NOW + timedelta(seconds=90)
    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(world.grant(ref), worker=WORKER)

    assert refused.value.code == "rate_limited"
    world.clock.now = NOW + timedelta(minutes=10)
    assert len(world.broker.redeem(world.grant(ref), worker=WORKER).reveal()) == 6


def test_rotate__replaces_the_secret_after_its_first_code() -> None:
    world = World()
    account = world.enrol()
    new = SecretValue("KRSXG5CTMVRXEZLUFAKE")
    code = world.codes.code(TotpParameters(new), at=NOW).reveal()

    with pytest.raises(SecretRefusedError):
        world.authenticator.rotate(account.id, new_secret=new, confirm_code="000000", by=owner())
    rotated = world.authenticator.rotate(account.id, new_secret=new, confirm_code=code, by=owner())
    shown = world.authenticator.current_code_for_display(rotated.id, viewer=owner())

    assert shown.code == world.codes.code(TotpParameters(new), at=NOW)
    assert SecretAuditAction.ROTATED in world.actions()


def test_remove__destroys_the_ciphertext() -> None:
    world = World()
    account = world.enrol()

    world.authenticator.remove(account.id, by=owner())

    assert world.authenticator.accounts[account.id].envelope is None
    with pytest.raises(SecretRefusedError):
        world.authenticator.current_code_for_display(account.id, viewer=owner())
    with pytest.raises(SecretRefusedError):
        world.authenticator.remove(account.id, by=owner())
    assert SecretAuditAction.REMOVED in world.actions()


def test_authenticator__another_organizations_user__not_found() -> None:
    world = World()
    account = world.enrol()
    stranger = AuthContext(
        user_id=uuid.uuid4(), assurance=AssuranceLevel.AAL2, session_id=None,
        memberships=(Membership(OTHER_ORG, Role.OWNER, frozenset()),), reauthenticated_at=NOW,
    )  # fmt: skip

    with pytest.raises(SecretRefusedError) as refused:
        world.authenticator.current_code_for_display(account.id, viewer=stranger)

    assert refused.value.code == "not_found"


# --- Never in logs or model inputs (AC-5.17.1) ---------------------------------------------------


@pytest.mark.ac("AC-5.17.1")
def test_scanner__blocks_a_model_input_with_a_released_code_or_secret() -> None:
    world = World()
    ref = world.enrol().ref
    code = world.broker.redeem(world.grant(ref), worker=WORKER).reveal()

    with pytest.raises(SecretInModelInputError):
        world.scanner.guard_model_input([f"The code is {code}."], now=world.clock())
    with pytest.raises(SecretInModelInputError):
        world.scanner.guard_model_input([f"secret={TOTP_SECRET}"], now=world.clock())
    with pytest.raises(SecretInModelInputError):
        world.scanner.guard_model_input(["otpauth://totp/Etsy?secret=X"], now=world.clock())
    world.scanner.guard_model_input(["Order 1234567 shipped."], now=world.clock())


@pytest.mark.ac("AC-5.17.1")
def test_scanner__a_code_is_forgotten_after_its_period() -> None:
    world = World()
    ref = world.enrol().ref
    code = world.broker.redeem(world.grant(ref), worker=WORKER).reveal()

    later = world.clock() + timedelta(minutes=10)

    assert not world.scanner.leaks(f"code {code}", now=later)


@pytest.mark.ac("AC-5.17.1")
def test_log_filter__masks_a_line_with_a_released_secret() -> None:
    world = World()
    grant = world.grant(world.store_password())
    value = world.broker.redeem(grant, worker=WORKER)
    logger = logging.getLogger("htco.test.secrets")
    log_filter = SecretLogFilter(world.scanner, world.clock)
    logger.addFilter(log_filter)

    try:
        with capture_logs() as logs:
            logger.warning("typed %s into the form", value.reveal())  # a bug that leaks it
            logger.warning("grant %s for %r", grant.grant_id, value)
    finally:
        logger.removeFilter(log_filter)

    assert PASSWORD not in logs.raw()
    assert "masked" in logs.raw()


@pytest.mark.ac("AC-5.17.1")
def test_errors__a_refusal_never_carries_the_value() -> None:
    world = World()
    grant = world.grant(world.store_password())
    world.broker.redeem(grant, worker=WORKER)

    with pytest.raises(SecretRefusedError) as refused:
        world.broker.redeem(grant, worker=WORKER)

    assert PASSWORD not in str(refused.value)
    assert PASSWORD not in repr(refused.value)


# --- The registry rule (AC-5.17.3) ---------------------------------------------------------------


@pytest.mark.ac("AC-5.17.3")
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"password": {"type": "string"}}},
        {"type": "object", "properties": {"totp_code": {"type": "string"}}},
        {"type": "object", "properties": {"one_time_code": {"type": "string"}}},
        {"type": "object", "properties": {"x": {"type": "string", "format": "password"}}},
        {"type": "object", "properties": {"x": {"type": "string", "writeOnly": True}}},
        {"type": "object", "properties": {"login": {"type": "object", "properties": {
            "otp": {"type": "string"}}}}},
        {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"access_token": {"type": "string"}}}}}},
    ],
)  # fmt: skip
def test_secret_fields__found_anywhere_in_an_output(schema: dict[str, Any]) -> None:
    assert secret_carrying_fields(schema)


@pytest.mark.ac("AC-5.17.3")
@pytest.mark.parametrize("name", ["currency_code", "error_code", "receipt_id", "postal_code"])
def test_secret_fields__ordinary_fields_pass(name: str) -> None:
    assert (
        secret_carrying_fields({"type": "object", "properties": {name: {"type": "string"}}}) == []
    )


@pytest.mark.ac("AC-5.17.3")
def test_registry__no_tool_and_no_action_output_can_carry_a_secret() -> None:
    registry = read_registry(REGISTRY).registry()
    catalog = real_catalog(registry)

    assert secret_tool_names(registry.tools) == []
    for definition in catalog.actions.values():
        assert secret_carrying_fields(definition.output_schema) == [], definition.name
    assert list(SCHEMAS.glob("*.output.json"))


@pytest.mark.ac("AC-5.17.3")
def test_registry__an_action_whose_output_carries_a_secret__refused() -> None:
    from htco_application.execution.contracts import ActionCatalog  # noqa: PLC0415

    registry = read_registry(REGISTRY).registry()
    tool = registry.tools["etsy.shop.read"]
    document = {
        "tool": tool.name, "approval_policy": "R0", "verify": "schema", "timeout_seconds": 10,
        "input_schema": "in.json", "output_schema": "out.json",
        "bindings": [{"channel": "internal", "recipe": "x"}],
    }  # fmt: skip
    schemas = {
        "in.json": {"type": "object", "properties": {}},
        "out.json": {"type": "object", "properties": {"etsy_password": {"type": "string"}}},
    }

    with pytest.raises(ActionCatalogError, match="secret"):
        ActionCatalog.from_documents([document], tools={tool.name: tool}, schemas=schemas)


def test_secret_tool_names__a_tool_that_returns_a_code__found() -> None:
    assert secret_tool_names(["auth.totp.read", "etsy.shop.read", "vault.password.get"]) == [
        "auth.totp.read",
        "vault.password.get",
    ]
