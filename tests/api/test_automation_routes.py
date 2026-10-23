"""Automation routes (slice 5.10) with bearer tokens, the in-memory store and engine."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_api.automations import AutomationServices
from htco_application.access.authentication import Membership
from htco_application.automation.durable import DurableAutomationEngine, StoredExecution
from htco_application.automation.records import ExecutionPage, ExecutionSummary
from htco_application.execution.gateway import UnavailableGateway
from htco_application.workforce.registry import Registry
from htco_domain.automation import AUTOMATION_SPEC_SECTIONS, ExecutionState
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.application.workforce_fixtures import (
    advisor_document,
    employee_document,
    tool_document,
)
from tests.auth_tokens import AUDIENCE, ISSUER, USER_ID, FakeJwks, claims
from tests.automation_fakes import EffectBook, MemoryExecutionStore, engine_document
from tests.idempotency_fakes import MemoryIdempotencyStore

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
ORG = uuid.UUID("5a000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("5a300000-0000-0000-0000-000000000001")
UNGRANTED = uuid.UUID("5a300000-0000-0000-0000-000000000002")
PATH = "/v1/automations/etsy-order-intake/executions"


class Reader:
    def __init__(self, store: MemoryExecutionStore) -> None:
        self.store = store
        self.asked: list[tuple[uuid.UUID, str, uuid.UUID | None, int]] = []

    async def list_executions(
        self,
        claims: Mapping[str, Any],
        organization_id: uuid.UUID,
        automation_id: str,
        *,
        before: uuid.UUID | None,
        limit: int,
    ) -> ExecutionPage:
        assert claims
        self.asked.append((organization_id, automation_id, before, limit))
        rows = sorted(
            (s.execution for s in self.store.rows.values()), key=lambda e: e.id, reverse=True
        )
        rows = [r for r in rows if before is None or r.id < before]
        items = tuple(
            ExecutionSummary(
                id=e.id,
                automation_id=e.automation_id,
                automation_version=e.automation_version,
                shop_id=e.shop_id,
                trigger=e.trigger,
                actor=e.actor,
                state=e.state,
                attempts=e.attempts,
                correlation_id=e.correlation_id,
                last_error=e.last_error,
                created_at=NOW,
                updated_at=NOW,
            )
            for e in rows[:limit]
        )
        return ExecutionPage(items, items[-1].id if len(rows) > limit else None)


class World:
    def __init__(self, role: Role = Role.OWNER, *, enabled: bool = True, **changes: Any) -> None:
        self.store = MemoryExecutionStore(shops={SHOP: ORG, UNGRANTED: ORG})
        self.book = EffectBook()
        registry = Registry.from_documents(
            employees=[employee_document()],
            tools=[tool_document()],
            automations=[engine_document(**changes)],
            advisors=[advisor_document()],
        )
        self.engine = DurableAutomationEngine(
            lambda: registry,
            self.store,
            {"etsy-order-intake": self.book.handle},
            clock=lambda: NOW,
            jitter=lambda: 0.5,
            gateway=UnavailableGateway(),
        )
        self.reader = Reader(self.store)
        services = AutomationServices(
            definitions=lambda: list(registry.automations.values()),
            executions=self.reader,
            shops=self.store,
            idempotency=MemoryIdempotencyStore(),
            engine=self.engine if enabled else None,
        )
        verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
        self.app = create_app(
            AppOptions(
                authenticator=Authenticator(
                    verifier, Memberships(Membership(ORG, role, frozenset({SHOP})))
                ),
                automations=services,
                clock=lambda: NOW,
            )
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {KEY.sign(claims(aal='aal2'))}"},
        )


def start(key: str = "k-1", **body: Any) -> dict[str, Any]:
    return {
        "json": {"shop_id": str(SHOP), "payload": {"receipt_id": "77"}} | body,
        "headers": {"Idempotency-Key": key},
    }


async def test_list_automations__the_current_definitions() -> None:
    world = World(Role.FINANCE)

    async with world.client() as http:
        answer = await http.get("/v1/automations")

    assert answer.status_code == 200
    [item] = answer.json()["items"]
    assert (item["id"], item["readiness"], item["user_triggerable"]) == (
        "etsy-order-intake",
        "active",
        True,
    )


async def test_list_automations__a_role_outside_the_office__403() -> None:
    async with World(Role.SUPPORT).client() as http:
        answer = await http.get("/v1/automations")

    assert answer.status_code == 403


@pytest.mark.ac("AC-5.10.1")
@pytest.mark.ac("AC-5.2.3")
async def test_start__same_idempotency_key__one_execution_and_the_stored_answer() -> None:
    world = World()

    async with world.client() as http:
        first = await http.post(PATH, **start())
        again = await http.post(PATH, **start())

    assert first.status_code == again.status_code == 202
    assert first.json() == again.json()
    assert first.json()["state"] == "pending"
    assert first.json()["trigger"] == "user_action"
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(world.store.rows) == 1


async def test_start__same_key_other_body__422_and_nothing_new() -> None:
    world = World()

    async with world.client() as http:
        await http.post(PATH, **start())
        reused = await http.post(PATH, **start(payload={"receipt_id": "78"}))

    assert reused.status_code == 422
    assert len(world.store.rows) == 1


async def test_start__without_an_idempotency_key__400() -> None:
    async with World().client() as http:
        answer = await http.post(PATH, json={"payload": {}})

    assert answer.status_code == 400
    assert answer.json()["type"].endswith("/idempotency-key-required")


@pytest.mark.ac("AC-5.10.3")
@pytest.mark.parametrize("role", [Role.FINANCE, Role.SUPPORT, Role.VIEWER])
async def test_start__a_role_without_the_capability__403(role: Role) -> None:
    world = World(role)

    async with world.client() as http:
        answer = await http.post(PATH, **start())

    assert answer.status_code == 403
    assert answer.json()["type"].endswith("/forbidden")
    assert world.store.rows == {}


@pytest.mark.ac("AC-5.10.3")
@pytest.mark.parametrize("shop", [UNGRANTED, uuid.UUID(int=3)])
async def test_start__a_shop_the_user_may_not_use__404(shop: uuid.UUID) -> None:
    world = World(Role.OPERATIONS)

    async with world.client() as http:
        answer = await http.post(PATH, **start(shop_id=str(shop)))

    assert answer.status_code == 404
    assert world.store.rows == {}


@pytest.mark.ac("AC-5.10.2")
async def test_start__an_automation_that_is_not_active__409_and_nothing_written() -> None:
    drafts = {name: {"status": "draft", "text": name} for name in AUTOMATION_SPEC_SECTIONS}
    world = World(readiness="draft", specification=drafts)

    async with world.client() as http:
        answer = await http.post(PATH, **start())

    assert answer.status_code == 409
    assert answer.json()["type"].endswith("/automation-refused")
    assert world.store.rows == {}


async def test_start__unknown_automation__404() -> None:
    async with World().client() as http:
        answer = await http.post("/v1/automations/nope-flow/executions", **start())

    assert answer.status_code == 404


async def test_start__automations_off__503() -> None:
    async with World(enabled=False).client() as http:
        answer = await http.post(PATH, **start())

    assert answer.status_code == 503
    assert answer.json()["type"].endswith("/automations-unavailable")


async def test_routes__without_the_automation_services__503() -> None:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                verifier, Memberships(Membership(ORG, Role.OWNER, frozenset()))
            )
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(aal='aal2'))}"},
    ) as http:
        answer = await http.get("/v1/automations")

    assert answer.status_code == 503


async def test_list_executions__newest_first_with_a_cursor() -> None:
    world = World()
    async with world.client() as http:
        for n in range(3):
            await http.post(PATH, **start(key=f"k-{n}"))
        first = await http.get(PATH, params={"limit": 2})
        second = await http.get(PATH, params={"limit": 2, "cursor": first.json()["next_cursor"]})

    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"] is not None
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    assert world.reader.asked[0][:2] == (ORG, "etsy-order-intake")


@pytest.mark.ac("AC-5.10.4")
async def test_retry__an_admin_sends_a_dead_letter_back_once() -> None:
    world = World(Role.ADMIN)
    async with world.client() as http:
        created = (await http.post(PATH, **start())).json()
        execution_id = uuid.UUID(created["id"])
        stored = world.store.rows[execution_id]
        dead = (
            stored.execution.transition(ExecutionState.RUNNING)
            .transition(ExecutionState.FAILED)
            .transition(ExecutionState.DEAD_LETTERED)
        )
        world.store.rows[execution_id] = StoredExecution(dead, stored.payload)
        retried = await http.post(f"{PATH}/{execution_id}/retry")
        again = await http.post(f"{PATH}/{execution_id}/retry")
        elsewhere = await http.post(f"/v1/automations/other-flow/executions/{execution_id}/retry")

    assert retried.status_code == 202
    assert retried.json()["state"] == "retry_scheduled"
    assert again.status_code == 409
    assert elsewhere.status_code == 404
    [audit] = world.store.audits
    assert audit.actor.id == str(USER_ID)


async def test_retry__operations__403() -> None:
    world = World(Role.OPERATIONS)
    async with world.client() as http:
        answer = await http.post(f"{PATH}/{uuid.uuid7()}/retry")

    assert answer.status_code == 403
