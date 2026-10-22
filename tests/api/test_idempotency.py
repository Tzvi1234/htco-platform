"""`Idempotency-Key` on unsafe routes (slice 5.2, AC-5.2.3), on a small app with one opted-in
POST route and the in-memory store. The real route (`POST /v1/automations/{id}/executions`) opts
in with slice 5.10."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from htco_api.idempotency import REPLAYED_HEADER, run_idempotently
from htco_api.problems import add_problem_handlers
from htco_application.idempotency import (
    InvalidIdempotencyKeyError,
    check_idempotency_key,
    is_storable,
    request_fingerprint,
)
from tests.idempotency_fakes import MemoryIdempotencyStore

pytestmark = pytest.mark.anyio

ALICE = uuid.UUID("2a000000-0000-0000-0000-000000000001")
BOB = uuid.UUID("2a000000-0000-0000-0000-000000000002")


class World:
    def __init__(self) -> None:
        self.store = MemoryIdempotencyStore()
        self.effects: list[dict[str, Any]] = []
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
        self.answer_status = 202
        self.app = FastAPI()
        add_problem_handlers(self.app)

        @self.app.post("/things")
        async def create_thing(request: Request) -> Any:
            user = uuid.UUID(request.headers.get("X-Test-User", str(ALICE)))

            async def work() -> tuple[int, dict[str, Any]]:
                body = await request.json()
                if body.get("explode"):
                    raise RuntimeError("boom")
                self.effects.append(body)
                return self.answer_status, {"thing": len(self.effects), "name": body["name"]}

            return await run_idempotently(
                request,
                user_id=user,
                store=self.store,
                now=self.now,
                work=work,
                required=request.headers.get("X-Test-Optional") is None,
            )


@pytest.fixture
def world() -> World:
    return World()


def client(world: World) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app, raise_app_exceptions=False),
        base_url="http://test",
    )


def headers(key: str, user: uuid.UUID = ALICE) -> dict[str, str]:
    return {"Idempotency-Key": key, "X-Test-User": str(user)}


@pytest.mark.ac("AC-5.2.3")
async def test_same_key_same_body__returns_the_stored_response_without_a_second_effect(
    world: World,
) -> None:
    async with client(world) as http:
        first = await http.post("/things", json={"name": "a"}, headers=headers("k-1"))
        again = await http.post("/things", json={"name": "a"}, headers=headers("k-1"))

    assert first.status_code == again.status_code == 202
    assert first.json() == again.json() == {"thing": 1, "name": "a"}
    assert again.headers[REPLAYED_HEADER] == "true"
    assert REPLAYED_HEADER not in first.headers
    assert len(world.effects) == 1


@pytest.mark.ac("AC-5.2.3")
async def test_same_key_other_body__is_refused_without_an_effect(world: World) -> None:
    async with client(world) as http:
        await http.post("/things", json={"name": "a"}, headers=headers("k-1"))
        reused = await http.post("/things", json={"name": "b"}, headers=headers("k-1"))

    assert reused.status_code == 422
    assert reused.json()["type"].endswith("/idempotency-key-reused")
    assert len(world.effects) == 1


async def test_same_key_while_the_first_still_runs__409(world: World) -> None:
    request = world.store
    async with client(world) as http:
        await http.post("/things", json={"name": "a"}, headers=headers("k-1"))
        # The first request of another key is still running (claimed, not completed).
        [slot] = list(request.rows)
        request.rows[slot].status = request.rows[slot].body = None
        running = await http.post("/things", json={"name": "a"}, headers=headers("k-1"))

    assert running.status_code == 409
    assert running.json()["type"].endswith("/idempotency-in-progress")


async def test_same_key_from_another_user__is_a_new_request(world: World) -> None:
    async with client(world) as http:
        await http.post("/things", json={"name": "a"}, headers=headers("k-1", ALICE))
        other = await http.post("/things", json={"name": "a"}, headers=headers("k-1", BOB))

    assert other.json() == {"thing": 2, "name": "a"}
    assert len(world.effects) == 2


async def test_after_24_hours__the_key_runs_again(world: World) -> None:
    async with client(world) as http:
        await http.post("/things", json={"name": "a"}, headers=headers("k-1"))
        world.now += timedelta(hours=24)
        later = await http.post("/things", json={"name": "a"}, headers=headers("k-1"))

    assert REPLAYED_HEADER not in later.headers
    assert len(world.effects) == 2


async def test_server_error__is_not_stored_and_the_retry_runs_for_real(world: World) -> None:
    world.answer_status = 503
    async with client(world) as http:
        await http.post("/things", json={"name": "a"}, headers=headers("k-1"))
        world.answer_status = 202
        retry = await http.post("/things", json={"name": "a"}, headers=headers("k-1"))

    assert retry.status_code == 202
    assert len(world.effects) == 2


async def test_work_that_raises__releases_the_key(world: World) -> None:
    async with client(world) as http:
        failed = await http.post("/things", json={"explode": True}, headers=headers("k-1"))

    assert failed.status_code == 500
    assert world.store.rows == {}


@pytest.mark.parametrize(
    ("sent", "problem"),
    [(None, "idempotency-key-required"), ("has space", "idempotency-key-invalid")],
)
async def test_missing_or_invalid_key__400(world: World, sent: str | None, problem: str) -> None:
    async with client(world) as http:
        answer = await http.post(
            "/things", json={"name": "a"}, headers={} if sent is None else {"Idempotency-Key": sent}
        )

    assert answer.status_code == 400
    assert answer.json()["type"].endswith(f"/{problem}")
    assert world.effects == []


async def test_optional_key_absent__runs_without_storing(world: World) -> None:
    async with client(world) as http:
        answer = await http.post("/things", json={"name": "a"}, headers={"X-Test-Optional": "1"})

    assert answer.status_code == 202
    assert world.store.rows == {}


@pytest.mark.parametrize("key", ["a", "x" * 255, "4b0f-~!_"])
def test_check_idempotency_key__visible_ascii_up_to_255__accepted(key: str) -> None:
    assert check_idempotency_key(key) == key


@pytest.mark.parametrize("key", ["", "x" * 256, "with space", "naïve", "tab\t"])
def test_check_idempotency_key__anything_else__rejected(key: str) -> None:
    with pytest.raises(InvalidIdempotencyKeyError):
        check_idempotency_key(key)


def test_request_fingerprint__differs_by_method_path_and_body() -> None:
    base = request_fingerprint("POST", "/a", b"{}")

    assert base == request_fingerprint("post", "/a", b"{}")
    assert len({base, request_fingerprint("PUT", "/a", b"{}")}) == 2
    assert base != request_fingerprint("POST", "/b", b"{}")
    assert base != request_fingerprint("POST", "/a", b"{ }")
    # Length-prefixed parts: moving bytes between path and body changes the hash.
    assert request_fingerprint("POST", "/ab", b"c") != request_fingerprint("POST", "/a", b"bc")


def test_is_storable__only_below_500() -> None:
    assert is_storable(201)
    assert is_storable(422)
    assert not is_storable(500)
    assert not is_storable(503)
