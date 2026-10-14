"""The owner bootstrap (slice 2.5, AC-2.5.1 to AC-2.5.3): one organization, one owner, audited."""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from htco_application.audit import AuditRecord
from htco_application.bootstrap import (
    BOOTSTRAP_ACTOR,
    BootstrapRequest,
    UserNotFoundError,
    bootstrap_owner,
)
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import Role

pytestmark = pytest.mark.anyio

USER = uuid.UUID("11111111-0000-0000-0000-000000000001")
CORRELATION = "corr-1"


@dataclass
class Store:
    """An in-memory database: one unit of work runs at a time and fails atomically."""

    users: dict[str, uuid.UUID] = field(default_factory=lambda: {"owner@example.test": USER})
    organizations: dict[uuid.UUID, str] = field(default_factory=dict)
    profiles: dict[uuid.UUID, str] = field(default_factory=dict)
    memberships: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, Role]] = field(default_factory=dict)
    audit: list[AuditRecord] = field(default_factory=list)
    fail_on_audit: bool = False

    async def run[T](self, work: Callable[[Store], T]) -> T:
        snapshot = (
            dict(self.organizations),
            dict(self.profiles),
            dict(self.memberships),
            list(self.audit),
        )
        try:
            return work(self)
        except Exception:
            self.organizations, self.profiles, self.memberships, self.audit = snapshot
            raise

    # The unit of work.
    def find_user_id(self, email: str) -> uuid.UUID | None:
        return self.users.get(email.lower())

    def owner_membership_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID] | None:
        for membership_id, (org, user, role) in self.memberships.items():
            if user == user_id and role is Role.OWNER:
                return org, membership_id
        return None

    def create_organization(self, organization_id: uuid.UUID, name: str) -> None:
        self.organizations[organization_id] = name

    def ensure_profile(self, user_id: uuid.UUID, display_name: str) -> None:
        self.profiles.setdefault(user_id, display_name)

    def create_membership(
        self, membership_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> None:
        self.memberships[membership_id] = (organization_id, user_id, role)

    def append_audit(self, record: AuditRecord) -> None:
        if self.fail_on_audit:
            raise RuntimeError("audit write failed")
        self.audit.append(record)


def ids() -> Callable[[], uuid.UUID]:
    sequence: Iterator[int] = iter(range(1, 100))
    return lambda: uuid.UUID(int=next(sequence))


REQUEST = BootstrapRequest(
    email="Owner@Example.test", organization_name="HT Commerce", display_name="Tzvi"
)


async def test_bootstrap__creates_the_organization_profile_and_owner_membership() -> None:
    store = Store()
    result = await bootstrap_owner(store, REQUEST, new_id=ids(), correlation_id=CORRELATION)

    assert result.created
    assert store.organizations == {result.organization_id: "HT Commerce"}
    assert store.profiles == {USER: "Tzvi"}
    assert store.memberships == {result.membership_id: (result.organization_id, USER, Role.OWNER)}


async def test_bootstrap__twice__one_organization_and_one_membership() -> None:
    """AC-2.5.1."""
    store = Store()
    first = await bootstrap_owner(store, REQUEST, new_id=ids(), correlation_id=CORRELATION)
    second = await bootstrap_owner(store, REQUEST, new_id=ids(), correlation_id="corr-2")

    assert not second.created
    assert (second.organization_id, second.membership_id) == (
        first.organization_id,
        first.membership_id,
    )
    assert len(store.organizations) == 1
    assert len(store.memberships) == 1
    assert len(store.audit) == 2  # nothing new the second time


async def test_bootstrap__audits_each_change_as_the_system_actor() -> None:
    """AC-2.5.2 and AC-2.5.3: actor, organization, entity, before, after, correlation ID."""
    store = Store()
    result = await bootstrap_owner(store, REQUEST, new_id=ids(), correlation_id=CORRELATION)

    organization, membership = store.audit
    assert ActorRef(ActorKind.SYSTEM, "bootstrap") == BOOTSTRAP_ACTOR
    assert organization.actor == BOOTSTRAP_ACTOR
    assert organization.organization_id == result.organization_id
    assert (organization.action, organization.entity_type) == (
        "organization.created",
        "organization",
    )
    assert organization.entity_id == str(result.organization_id)
    assert organization.before is None
    assert organization.after == {"name": "HT Commerce"}
    assert organization.correlation_id == CORRELATION

    assert membership.actor == BOOTSTRAP_ACTOR
    assert (membership.action, membership.entity_type) == ("membership.created", "membership")
    assert membership.entity_id == str(result.membership_id)
    assert membership.before is None
    assert membership.after == {"user_id": str(USER), "role": "owner"}
    assert membership.correlation_id == CORRELATION
    assert len({record.id for record in store.audit}) == 2


async def test_bootstrap__unknown_email__refused_and_nothing_written() -> None:
    store = Store()
    with pytest.raises(UserNotFoundError):
        await bootstrap_owner(
            store,
            BootstrapRequest(email="nobody@example.test", organization_name="X", display_name="X"),
            new_id=ids(),
            correlation_id=CORRELATION,
        )
    assert store.organizations == {}
    assert store.audit == []


async def test_bootstrap__failing_audit__writes_nothing() -> None:
    store = Store(fail_on_audit=True)
    with pytest.raises(RuntimeError):
        await bootstrap_owner(store, REQUEST, new_id=ids(), correlation_id=CORRELATION)
    assert store.organizations == {}
    assert store.memberships == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"email": "  "},
        {"email": "not-an-email"},
        {"organization_name": " "},
        {"organization_name": "x" * 201},
        {"display_name": ""},
    ],
)
def test_bootstrap_request__invalid_values__are_refused(fields: dict[str, Any]) -> None:
    values = {"email": "owner@example.test", "organization_name": "HT", "display_name": "T"}
    with pytest.raises(ValueError, match=r"email|name"):
        BootstrapRequest(**(values | fields))


def test_audit_record__needs_a_nonblank_action_and_entity() -> None:
    org = uuid.uuid4()
    with pytest.raises(ValueError, match="action"):
        AuditRecord.new(
            organization_id=org, actor=BOOTSTRAP_ACTOR, action=" ", entity_type="x", entity_id="1"
        )
    with pytest.raises(ValueError, match="entity_type"):
        AuditRecord.new(
            organization_id=org, actor=BOOTSTRAP_ACTOR, action="a", entity_type="", entity_id="1"
        )
