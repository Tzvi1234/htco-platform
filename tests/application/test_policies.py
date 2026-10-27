"""Versioned policies and recorded decisions (slice 7.2, AC-7.2.3).

A policy is never edited in place: publishing a change creates the next version, and every
decision is recorded with the version it was made under, so an old decision keeps its version.
Only the Owner, at `aal2` with a recent step-up, publishes a change ("Edit model policies,
budgets, kill switches"), and each change is audited.
"""

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.policy import ShopRef
from htco_application.audit import AuditRecord
from htco_application.policies import (
    POLICY_PUBLISHED,
    PolicyEditDeniedError,
    PolicyRequest,
    PolicyService,
    RecordedDecision,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.policy import (
    ActorGrants,
    CapabilitySpec,
    PolicyDocument,
    PolicyVersion,
    Reason,
)
from htco_domain.tooling import RiskLevel, SideEffect
from tests.application.test_access_policy import NOW

ORG = uuid.UUID("00000000-0000-7000-8000-0000000000a0")
OTHER_ORG = uuid.UUID("00000000-0000-7000-8000-0000000000b0")
SHOP = ShopRef(uuid.UUID("00000000-0000-7000-8000-00000000000a"), organization_id=ORG)
OWNER_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")

READ = CapabilitySpec(SideEffect.NONE, RiskLevel.LOW)
INTERNAL = CapabilitySpec(SideEffect.INTERNAL, RiskLevel.LOW)
V1 = PolicyDocument(
    capabilities={"orders.read": READ, "tasks.update": INTERNAL},
    automations={"tracking-sync": ActorGrants(frozenset({"orders.read"}))},
)
V2 = PolicyDocument(
    capabilities={"orders.read": READ, "tasks.update": INTERNAL},
    automations={"tracking-sync": ActorGrants(frozenset({"orders.read", "tasks.update"}))},
)
SYNC = ActorRef(ActorKind.AUTOMATION, "tracking-sync")


@dataclass
class FakePolicyStore:
    versions: list[PolicyVersion] = field(default_factory=list)
    decisions: list[RecordedDecision] = field(default_factory=list)
    audits: list[AuditRecord] = field(default_factory=list)

    def latest(self, organization_id: uuid.UUID) -> PolicyVersion | None:
        mine = [v for v in self.versions if v.organization_id == organization_id]
        return max(mine, key=lambda v: v.version, default=None)

    def publish(  # noqa: PLR0913 - the store's signature
        self,
        organization_id: uuid.UUID,
        document: PolicyDocument,
        *,
        created_by: ActorRef,
        now: datetime,
        audit: Callable[[int], AuditRecord],
        only_if_none: bool = False,
    ) -> PolicyVersion:
        latest = self.latest(organization_id)
        if only_if_none and latest is not None:
            return latest
        version = PolicyVersion(
            organization_id,
            1 if latest is None else latest.version + 1,
            document,
            created_by,
            now,
        )
        self.versions.append(version)
        self.audits.append(audit(version.version))
        return version

    def record(self, decision: RecordedDecision) -> None:
        self.decisions.append(decision)


def owner(
    *,
    role: Role = Role.OWNER,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    reauthenticated_minutes_ago: int | None = 1,
) -> AuthContext:
    return AuthContext(
        user_id=OWNER_ID,
        assurance=assurance,
        session_id="s-1",
        memberships=(Membership(ORG, role, frozenset()),),
        reauthenticated_at=(
            None
            if reauthenticated_minutes_ago is None
            else NOW - timedelta(minutes=reauthenticated_minutes_ago)
        ),
    )


def a_request(capability: str = "tasks.update") -> PolicyRequest:
    return PolicyRequest(organization_id=ORG, actor=SYNC, capability=capability, shop=SHOP, now=NOW)


@pytest.fixture
def store() -> FakePolicyStore:
    return FakePolicyStore()


@pytest.fixture
def service(store: FakePolicyStore) -> PolicyService:
    return PolicyService(store)


# --- versions ----------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.ac("AC-7.2.3")
async def test_publish__a_change_creates_the_next_version(
    service: PolicyService, store: FakePolicyStore
) -> None:
    first = await service.ensure_baseline(ORG, V1, now=NOW)

    second = await service.publish(owner(), ORG, V2, now=NOW)

    assert (first.version, second.version) == (1, 2)
    assert [v.document for v in store.versions] == [V1, V2]
    assert second.created_by == ActorRef(ActorKind.USER, str(OWNER_ID))


@pytest.mark.anyio
@pytest.mark.ac("AC-7.2.3")
async def test_decide__old_decisions_keep_their_version(
    service: PolicyService, store: FakePolicyStore
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)
    before = await service.decide(a_request(), correlation_id="c-1")

    await service.publish(owner(), ORG, V2, now=NOW)
    after = await service.decide(a_request(), correlation_id="c-2")

    assert (before.reason, before.policy_version) == (Reason.NOT_GRANTED, 1)
    assert (after.reason, after.policy_version) == (Reason.GRANTED, 2)
    assert [(d.decision.policy_version, d.correlation_id) for d in store.decisions] == [
        (1, "c-1"),
        (2, "c-2"),
    ]


@pytest.mark.anyio
async def test_decide__every_decision_is_recorded_with_its_shop_and_time(
    service: PolicyService, store: FakePolicyStore
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)

    decision = await service.decide(a_request("orders.read"), correlation_id="c-1")

    (recorded,) = store.decisions
    assert recorded.decision == decision
    assert (recorded.organization_id, recorded.shop_id, recorded.decided_at) == (ORG, SHOP.id, NOW)


@pytest.mark.anyio
async def test_decide__a_shop_of_another_tenant_is_never_recorded(
    service: PolicyService, store: FakePolicyStore
) -> None:
    """Codex on #189: a request naming another tenant's shop is refused, and the recorded
    denial keeps only the reason, never that tenant's shop ID in this organization's trail."""
    await service.ensure_baseline(ORG, V1, now=NOW)
    foreign = ShopRef(uuid.UUID("00000000-0000-7000-8000-00000000000f"), organization_id=OTHER_ORG)
    request = dataclasses.replace(a_request("orders.read"), shop=foreign)

    decision = await service.decide(request, correlation_id="c-1")

    (recorded,) = store.decisions
    assert decision.reason is Reason.OTHER_ORGANIZATION
    assert (recorded.organization_id, recorded.shop_id) == (ORG, None)


@pytest.mark.anyio
async def test_decide__without_a_policy_denies_and_records_no_version(
    service: PolicyService, store: FakePolicyStore
) -> None:
    decision = await service.decide(a_request("orders.read"), correlation_id="c-1")

    assert decision.reason is Reason.NO_POLICY
    assert store.decisions[0].decision.policy_version is None


@pytest.mark.anyio
async def test_decide__a_long_correlation_id_is_cut_to_the_column(
    service: PolicyService, store: FakePolicyStore
) -> None:
    await service.decide(a_request(), correlation_id="c" * 500)

    assert len(store.decisions[0].correlation_id) == 200


@pytest.mark.anyio
async def test_ensure_baseline__publishes_only_the_first_version(
    service: PolicyService, store: FakePolicyStore
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)

    again = await service.ensure_baseline(ORG, V2, now=NOW)

    assert again.version == 1
    assert again.document == V1
    assert len(store.versions) == 1
    assert store.audits[0].actor == ActorRef(ActorKind.SYSTEM, "policy-baseline")


@pytest.mark.anyio
async def test_active__is_the_latest_version_of_that_organization(
    service: PolicyService,
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)
    await service.publish(owner(), ORG, V2, now=NOW)

    assert (await service.active(ORG)).version == 2  # type: ignore[union-attr]
    assert await service.active(OTHER_ORG) is None


@pytest.mark.anyio
async def test_publish__reverting_publishes_the_old_document_as_a_new_version(
    service: PolicyService,
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)
    await service.publish(owner(), ORG, V2, now=NOW)

    reverted = await service.publish(owner(), ORG, V1, now=NOW)

    assert (reverted.version, reverted.document) == (3, V1)


# --- who may publish -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_publish__is_audited_with_the_old_and_new_version(
    service: PolicyService, store: FakePolicyStore
) -> None:
    await service.ensure_baseline(ORG, V1, now=NOW)

    await service.publish(owner(), ORG, V2, now=NOW, correlation_id="req-1")

    audit = store.audits[-1]
    assert (audit.action, audit.entity_type, audit.entity_id) == (POLICY_PUBLISHED, "policy", "2")
    assert audit.actor == ActorRef(ActorKind.USER, str(OWNER_ID))
    assert audit.organization_id == ORG
    assert (audit.before, audit.after) == ({"version": 1}, {"version": 2})
    assert audit.correlation_id == "req-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (owner(role=Role.ADMIN), Reason.NOT_GRANTED),
        (owner(role=Role.DEVELOPER), Reason.NOT_GRANTED),
        (owner(assurance=AssuranceLevel.AAL1), Reason.MFA_REQUIRED),
        (owner(reauthenticated_minutes_ago=None), Reason.STEP_UP_REQUIRED),
        (owner(reauthenticated_minutes_ago=11), Reason.STEP_UP_REQUIRED),
    ],
)
async def test_publish__only_a_stepped_up_owner(
    service: PolicyService, store: FakePolicyStore, context: AuthContext, reason: Reason
) -> None:
    with pytest.raises(PolicyEditDeniedError) as denied:
        await service.publish(context, ORG, V2, now=NOW)

    assert denied.value.reason is reason
    assert store.versions == []


@pytest.mark.anyio
async def test_publish__a_non_member_is_refused(
    service: PolicyService, store: FakePolicyStore
) -> None:
    with pytest.raises(PolicyEditDeniedError) as denied:
        await service.publish(owner(), OTHER_ORG, V2, now=NOW)

    assert denied.value.reason is Reason.UNKNOWN_ACTOR
    assert store.versions == []


@pytest.mark.anyio
async def test_publish__needs_a_timezone(service: PolicyService) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await service.publish(owner(), ORG, V2, now=NOW.replace(tzinfo=None))
