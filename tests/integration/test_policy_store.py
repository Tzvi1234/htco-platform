"""Policies on real Postgres (slice 7.2): concurrent changes get consecutive versions, each with
its audit record; the baseline is published once; decisions keep the version they were made under
(AC-7.2.3); the worker reads its job's policy and records its job's decisions only."""

import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import pytest

from htco_application.access.engine import PolicyRequest
from htco_application.access.policy import ShopRef
from htco_application.audit import AuditRecord
from htco_application.policies import BASELINE_ACTOR, PolicyService
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.policy import ActorGrants, CapabilitySpec, PolicyDocument, Reason
from htco_domain.tooling import RiskLevel, SideEffect
from htco_persistence.database import Database, tenant_scope
from htco_persistence.policies import PostgresPolicyStore
from tests.integration.test_etsy_connection import _url, scalar

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
EDITOR = ActorRef(ActorKind.USER, "00000000-0000-7000-8000-000000000001")
SYNC = ActorRef(ActorKind.AUTOMATION, "tracking-sync")
READ = CapabilitySpec(SideEffect.NONE, RiskLevel.LOW)
WRITE = CapabilitySpec(SideEffect.EXTERNAL, RiskLevel.HIGH)
V1 = PolicyDocument(
    capabilities={"orders.read": READ, "etsy.tracking.upload": WRITE},
    automations={"tracking-sync": ActorGrants(frozenset({"orders.read"}))},
)
V2 = PolicyDocument(
    capabilities={"orders.read": READ, "etsy.tracking.upload": WRITE},
    automations={
        "tracking-sync": ActorGrants(
            frozenset({"orders.read", "etsy.tracking.upload"}),
            pre_approved=frozenset({"etsy.tracking.upload"}),
        )
    },
)


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=8)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


@pytest.fixture
async def worker_db() -> AsyncIterator[Database]:
    db = Database(_url(), min_size=1, max_size=2, role="htco_worker")
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def organization(admin: Database) -> tuple[uuid.UUID, uuid.UUID]:
    org, shop = uuid.uuid4(), uuid.uuid4()
    with admin.connection() as connection:
        connection.execute(
            "insert into app.organizations (id, name) values (%s, %s)", [org, f"Policies {org}"]
        )
        connection.execute(
            "insert into app.shops (id, organization_id, name, marketplace, status) "
            "values (%s, %s, 'Shop', 'etsy', 'active')",
            [shop, org],
        )
    return org, shop


def audit_for(org: uuid.UUID) -> Callable[[int], AuditRecord]:
    def build(version: int) -> AuditRecord:
        return AuditRecord.new(
            organization_id=org,
            actor=EDITOR,
            action="policies.published",
            entity_type="policy",
            entity_id=str(version),
            after={"version": version},
        )

    return build


def a_request(org: uuid.UUID, shop: uuid.UUID, capability: str) -> PolicyRequest:
    return PolicyRequest(
        organization_id=org,
        actor=SYNC,
        capability=capability,
        shop=ShopRef(shop, organization_id=org),
        now=NOW,
    )


async def test_publish__concurrent_changes_get_consecutive_versions_with_audits(
    admin: Database,
) -> None:
    org, _ = organization(admin)
    store = PostgresPolicyStore(admin)

    def publish(document: PolicyDocument) -> int:
        return store.publish(
            org, document, created_by=EDITOR, now=NOW, audit=audit_for(org)
        ).version

    with ThreadPoolExecutor(max_workers=6) as pool:
        versions = sorted(pool.map(publish, [V1, V2] * 3))

    assert versions == [1, 2, 3, 4, 5, 6]
    audits = scalar(
        admin,
        "select count(*) from audit.audit_log where organization_id = %s "
        "and action = 'policies.published'",
        [org],
    )
    assert audits == 6


async def test_publish__round_trips_the_document(admin: Database) -> None:
    org, _ = organization(admin)
    store = PostgresPolicyStore(admin)

    published = store.publish(org, V2, created_by=EDITOR, now=NOW, audit=audit_for(org))

    latest = store.latest(org)
    assert latest == published
    assert latest is not None
    assert (latest.document, latest.created_by, latest.created_at) == (V2, EDITOR, NOW)


async def test_ensure_baseline__is_published_once(admin: Database) -> None:
    org, _ = organization(admin)
    service = PolicyService(PostgresPolicyStore(admin))

    first = await service.ensure_baseline(org, V1, now=NOW)
    again = await service.ensure_baseline(org, V2, now=NOW)

    assert (first.version, again.version, again.document) == (1, 1, V1)
    assert first.created_by == BASELINE_ACTOR
    assert scalar(admin, "select count(*) from app.policies where organization_id = %s", [org]) == 1


@pytest.mark.ac("AC-7.2.3")
async def test_decisions__keep_the_version_they_were_made_under(admin: Database) -> None:
    org, shop = organization(admin)
    store = PostgresPolicyStore(admin)
    service = PolicyService(store)
    await service.ensure_baseline(org, V1, now=NOW)

    before = await service.decide(
        a_request(org, shop, "etsy.tracking.upload"), correlation_id="c-1"
    )
    store.publish(org, V2, created_by=EDITOR, now=NOW, audit=audit_for(org))
    after = await service.decide(a_request(org, shop, "etsy.tracking.upload"), correlation_id="c-2")

    assert (before.reason, after.reason) == (Reason.NOT_GRANTED, Reason.PRE_APPROVED)
    with admin.connection() as connection:
        rows = connection.execute(
            "select correlation_id, policy_version, verdict, reason, shop_id "
            "from app.policy_decisions where organization_id = %s order by correlation_id",
            [org],
        ).fetchall()
    assert rows == [
        ("c-1", 1, "deny", "not_granted", shop),
        ("c-2", 2, "allow", "pre_approved", shop),
    ]


async def test_decisions__without_a_policy_are_recorded_as_denials(admin: Database) -> None:
    org, shop = organization(admin)
    service = PolicyService(PostgresPolicyStore(admin))

    decision = await service.decide(a_request(org, shop, "x" * 300), correlation_id="c-1")

    assert decision.reason is Reason.NO_POLICY
    stored = scalar(
        admin,
        "select char_length(capability) from app.policy_decisions where organization_id = %s",
        [org],
    )
    assert stored == 200


async def test_worker__reads_its_jobs_policy_and_records_its_decisions(
    admin: Database, worker_db: Database
) -> None:
    org, shop = organization(admin)
    other, _ = organization(admin)
    store = PostgresPolicyStore(admin)
    store.publish(org, V1, created_by=EDITOR, now=NOW, audit=audit_for(org))
    store.publish(other, V2, created_by=EDITOR, now=NOW, audit=audit_for(other))
    worker = PolicyService(PostgresPolicyStore(worker_db))

    with tenant_scope(org):
        decision = await worker.decide(a_request(org, shop, "orders.read"), correlation_id="w-1")
        assert (await worker.active(other)) is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            PostgresPolicyStore(worker_db).publish(
                org, V2, created_by=EDITOR, now=NOW, audit=audit_for(org)
            )

    assert (decision.reason, decision.policy_version) == (Reason.GRANTED, 1)
    assert scalar(
        admin, "select count(*) from app.policy_decisions where correlation_id = 'w-1'", []
    ) == 1  # fmt: skip
