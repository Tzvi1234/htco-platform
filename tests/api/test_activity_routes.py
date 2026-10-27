"""The activity query API (slice 7.8): `/v1/activity` and `/v1/tasks/{id}/activity`, with the
in-memory reader of the use-case tests."""

import uuid
from datetime import timedelta

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import Membership
from htco_domain.activity import ActivityOutcome
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.application.test_activity_query import (
    INTAKE,
    NOW,
    ORG,
    OTHER_SHOP,
    SHON,
    SHOP,
    SYNC,
    TASK,
    MemoryActivity,
    record,
)
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims

pytestmark = pytest.mark.anyio


def client(
    reader: MemoryActivity | None,
    role: Role = Role.OWNER,
    shops: frozenset[uuid.UUID] = frozenset({SHOP}),
) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, Memberships(Membership(ORG, role, shops))),
            activity_reader=reader,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY.sign(claims())}"},
    )


@pytest.mark.ac("AC-7.8.1")
async def test_ac_7_8_1_an_actor_today_in_the_users_zone() -> None:
    # 23:30 UTC on the 26th is the 27th in Jerusalem; 22:30 on the 27th is the 28th there.
    reader = MemoryActivity(
        [
            today := record(NOW.replace(day=26, hour=23, minute=30)),
            record(NOW.replace(day=27, hour=22, minute=30)),
            record(NOW, actor=SYNC),
            record(NOW, shop=OTHER_SHOP),
        ]
    )
    async with client(reader, Role.OPERATIONS) as http:
        response = await http.get(
            "/v1/activity",
            params={"actor": "automation:etsy-order-intake", "day": "2026-10-27",
                    "timezone": "Asia/Jerusalem"},
        )  # fmt: skip

    assert response.status_code == 200
    body = response.json()
    assert [i["id"] for i in body["items"]] == [str(today.id)]
    assert body["items"][0]["actor"] == "automation:etsy-order-intake"
    assert body["source"] == "app.activity_records"
    assert body["read_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert body["empty_reason"] is None
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.ac("AC-7.8.2")
async def test_ac_7_8_2_failed_automations_in_a_range() -> None:
    failed = record(NOW - timedelta(days=1), outcome=ActivityOutcome.FAILED)
    reader = MemoryActivity([failed, record(NOW - timedelta(days=2))])
    async with client(reader) as http:
        response = await http.get(
            "/v1/activity",
            params={"actor_kind": "automation", "outcome": "failed",
                    "since": (NOW - timedelta(days=7)).isoformat(), "until": NOW.isoformat()},
        )  # fmt: skip

    assert [i["id"] for i in response.json()["items"]] == [str(failed.id)]
    assert response.json()["items"][0]["correlation_id"] == "c-1"


@pytest.mark.ac("AC-7.8.3")
async def test_ac_7_8_3_a_tasks_trail() -> None:
    first = record(NOW - timedelta(minutes=2), actor=SHON, correlation="c-7", task=TASK)
    second = record(NOW - timedelta(minutes=1), actor=INTAKE, correlation="c-7")
    async with client(MemoryActivity([second, first])) as http:
        response = await http.get(f"/v1/tasks/{TASK}/activity")

    assert [i["id"] for i in response.json()["items"]] == [str(first.id), str(second.id)]


@pytest.mark.ac("AC-7.8.4")
async def test_ac_7_8_4_an_empty_result_says_no_recorded_activity() -> None:
    async with client(MemoryActivity()) as http:
        response = await http.get("/v1/activity")

    assert response.json() == {
        "items": [], "next_cursor": None, "source": "app.activity_records",
        "read_at": NOW.isoformat().replace("+00:00", "Z"), "empty_reason": "no_recorded_activity",
    }  # fmt: skip


async def test_the_cursor_pages_through() -> None:
    rows = [record(NOW - timedelta(minutes=m)) for m in range(3)]
    async with client(MemoryActivity(rows)) as http:
        first = (await http.get("/v1/activity", params={"limit": 2})).json()
        second = (
            await http.get("/v1/activity", params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    assert [i["id"] for i in first["items"] + second["items"]] == [str(r.id) for r in rows]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "params",
    [{"actor": "robot"}, {"actor": "automation:"}, {"cursor": "not-a-cursor"},
     {"day": "2026-10-27", "timezone": "Mars/Base"},
     {"day": "2026-10-27", "since": NOW.isoformat()},
     {"since": NOW.isoformat(), "until": (NOW - timedelta(hours=1)).isoformat()},
     {"since": "2026-10-27T10:00:00"}],
)  # fmt: skip
async def test_bad_queries_are_422(params: dict[str, str]) -> None:
    async with client(MemoryActivity()) as http:
        response = await http.get("/v1/activity", params=params)

    assert response.status_code == 422


async def test_a_role_without_the_capability_is_403() -> None:
    async with client(MemoryActivity(), Role.VIEWER) as http:
        assert (await http.get("/v1/activity")).status_code == 403
        assert (await http.get(f"/v1/tasks/{TASK}/activity")).status_code == 403


async def test_without_a_reader_the_api_says_so() -> None:
    async with client(None) as http:
        assert (await http.get("/v1/activity")).status_code == 503


@pytest.mark.ac("AC-7.8.3")
async def test_a_tasks_trail_over_http_keeps_the_shop_scope() -> None:
    """Quill2 on #200: an Operations member granted shop A gets the trail's shop A records only."""
    in_a = record(NOW - timedelta(minutes=3), task=TASK, correlation="c-t")
    reader = MemoryActivity(
        [
            in_a,
            record(NOW - timedelta(minutes=2), shop=OTHER_SHOP, correlation="c-t"),
            record(NOW - timedelta(minutes=1), shop=None, correlation="c-t"),
        ]
    )
    async with client(reader, Role.OPERATIONS, frozenset({SHOP})) as http:
        response = await http.get(f"/v1/tasks/{TASK}/activity")

    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == [str(in_a.id)]


@pytest.mark.ac("AC-7.8.3")
async def test_a_long_trail_over_http_continues_with_a_cursor() -> None:
    rows = [
        record(NOW - timedelta(minutes=500 - i), task=TASK, correlation="c-t") for i in range(3)
    ]
    async with client(MemoryActivity(rows)) as http:
        first = await http.get(f"/v1/tasks/{TASK}/activity", params={"limit": 2})
        cursor = first.json()["next_cursor"]
        rest = await http.get(f"/v1/tasks/{TASK}/activity", params={"limit": 2, "cursor": cursor})
        bad = await http.get(f"/v1/tasks/{TASK}/activity", params={"cursor": "not-a-cursor"})

    assert cursor is not None
    ids = [i["id"] for i in (*first.json()["items"], *rest.json()["items"])]
    assert ids == [str(r.id) for r in rows]
    assert rest.json()["next_cursor"] is None
    assert bad.status_code == 422
