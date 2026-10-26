"""`GET /v1/executions/{execution_id}` (slice 5.18): the chain view."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import Membership
from htco_application.status import ChainRecords
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.application.test_status import ORG, SHOP, order_records
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class Reader:
    def __init__(self, records: ChainRecords | None) -> None:
        self.records_ = records

    def records(
        self,
        organization_id: uuid.UUID,  # noqa: ARG002
        execution_id: uuid.UUID,  # noqa: ARG002
    ) -> ChainRecords | None:
        return self.records_


def client(
    role: Role = Role.OWNER, *, records: ChainRecords | None = None, wired: bool = True
) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(
                verifier, Memberships(Membership(ORG, role, frozenset({SHOP})))
            ),
            execution_chain=Reader(records) if wired else None,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


@pytest.mark.ac("AC-5.18.1")
async def test_get__the_chain_in_causal_order() -> None:
    records = order_records()

    async with client(records=records) as http:
        answer = await http.get(f"/v1/executions/{records.execution.id}")

    assert answer.status_code == 200
    body = answer.json()
    assert (body["automation_id"], body["state"], body["status"]) == (
        "etsy-order-intake",
        "succeeded",
        "succeeded",
    )
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds[0] == "trigger"
    assert kinds.count("action") == 2
    assert answer.headers["Cache-Control"] == "no-store"


async def test_get__unknown__404() -> None:
    async with client(records=None) as http:
        answer = await http.get(f"/v1/executions/{uuid.uuid4()}")

    assert answer.status_code == 404


async def test_get__a_role_outside_the_office__403() -> None:
    async with client(Role.SUPPORT, records=order_records()) as http:
        answer = await http.get(f"/v1/executions/{uuid.uuid4()}")

    assert answer.status_code == 403


async def test_get__not_wired__503() -> None:
    async with client(wired=False) as http:
        answer = await http.get(f"/v1/executions/{uuid.uuid4()}")

    assert answer.status_code == 503


@pytest.mark.parametrize(
    ("role", "output"),
    [(Role.OWNER, {"receipts": 2, "recorded": 1}), (Role.ADMIN, {"receipts": 2, "recorded": 1}),
     (Role.OPERATIONS, None)],
)  # fmt: skip
async def test_get__the_run_output_for_the_owner_and_admins_only(
    role: Role, output: dict[str, int] | None
) -> None:
    """Per-run counts (#127): shown to the Owner and Admins, null for a shop-scoped role."""
    records = order_records(output={"receipts": 2, "recorded": 1})

    async with client(role, records=records) as http:
        answer = await http.get(f"/v1/executions/{records.execution.id}")

    assert answer.status_code == 200
    assert answer.json()["output"] == output
