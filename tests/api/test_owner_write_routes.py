"""The Owner's write actions routes (slices 6.6 part B and 6.8), with the real gateway over fakes.

`/v1/shops/{shop_id}/owner-writes...` and `/v1/relay-items/{item_id}/upload...`: 403 without the
capability, the second factor, the step-up, the scope or the flag (AC-6.6.1, AC-6.6.3, AC-6.8.2),
`already_present` when the receipt has the code (AC-6.6.2), no code in any response or log.
"""

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any, override

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import AuthContext, Membership
from htco_application.marketplace import OrderShipment
from htco_application.relay import RelayItem
from htco_domain.identity import Role
from htco_domain.relay import RelayState
from htco_platform.jwt import JwtVerifier
from tests.api.test_me_and_shops import KEY, Memberships
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, claims
from tests.log_capture import capture_logs
from tests.order_intake_fakes import paid_receipt
from tests.owner_write_fakes import NOW, ORG, RECEIPT, SHOP, World

pytestmark = pytest.mark.anyio

PROBLEMS = "https://ht-co.app/problems/"
CODE = "LX123456789IL"
PASTE = {"receipt_id": RECEIPT, "carrier": "israel_post", "code": CODE}
BASE = f"/v1/shops/{SHOP}/owner-writes"


class SteppedUp(Authenticator):
    """A bearer caller whose session re-entered its second factor `ago` before NOW (the web
    session's step-up, carried here without the cookie flow)."""

    def __init__(self, role: Role, ago: timedelta | None) -> None:
        super().__init__(
            JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY)),
            Memberships(Membership(ORG, role, frozenset({SHOP}))),
        )
        self._ago = ago

    @override
    async def authenticate(self, authorization: str | None) -> AuthContext:
        found = await super().authenticate(authorization)
        return replace(found, reauthenticated_at=None if self._ago is None else NOW - self._ago)


def client(
    world: World | None,
    role: Role = Role.OWNER,
    *,
    aal: str = "aal2",
    stepped_up: timedelta | None = timedelta(minutes=1),
) -> httpx.AsyncClient:
    app = create_app(
        AppOptions(
            authenticator=SteppedUp(role, stepped_up),
            owner_writes=None if world is None else world.services,
            clock=lambda: NOW,
        )
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(aal=aal))}"},
    )


def press_headers(key: str = "press-1") -> dict[str, str]:
    return {"Idempotency-Key": key}


async def test_the_console__the_owners_writes_off_by_default() -> None:
    world = World(flags=frozenset(), scopes=frozenset({"transactions_r"}))
    async with client(world) as http:
        answer = await http.get(BASE)
    assert answer.status_code == 200
    assert answer.headers["Cache-Control"] == "no-store"
    rows = answer.json()["items"]
    assert [r["key"] for r in rows] == [
        "tracking_upload", "vacation_mode", "mark_shipped", "holiday_preference",
    ]  # fmt: skip
    assert all(not r["enabled"] and r["reconsent_required"] for r in rows)
    assert rows[0]["reconnect_url"] == "/v1/shops/etsy/connect"


async def test_the_console__an_admin_403() -> None:
    async with client(World(), Role.ADMIN) as http:
        answer = await http.get(BASE)
    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + "forbidden"


async def test_the_routes__503_when_not_set_up() -> None:
    async with client(None) as http:
        answer = await http.get(BASE)
    assert answer.status_code == 503
    assert answer.json()["type"] == PROBLEMS + "owner-writes-unavailable"


async def test_the_preview__no_code_and_no_call() -> None:
    world = World()
    async with client(world) as http:
        answer = await http.post(f"{BASE}/tracking-upload/preview", json=PASTE)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["target"] == {"receipt_id": RECEIPT, "carrier": "israel_post", "last4": "89IL"}
    assert body["can_run"] is True
    assert CODE not in answer.text
    assert world.etsy.calls == []


async def test_the_preview__a_wrong_format_422_without_the_code() -> None:
    async with client(World()) as http:
        answer = await http.post(f"{BASE}/tracking-upload/preview", json={**PASTE, "code": "ZZ99"})
    assert answer.status_code == 422
    assert answer.json()["type"] == PROBLEMS + "write-input-invalid"
    assert "ZZ99" not in answer.text


@pytest.mark.ac("AC-6.6.3")
async def test_the_press__uploads_once_and_replays_the_same_key() -> None:
    world = World()
    async with client(world) as http:
        with capture_logs() as logs:
            first = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
            again = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["outcome"], body["verified"], body["sent"], body["already_present"]) == (
        "succeeded", True, True, False,
    )  # fmt: skip
    assert body["write"]["last_run"]["execution_id"] == body["execution_id"]
    assert again.json()["execution_id"] == body["execution_id"]
    assert again.headers["Idempotent-Replayed"] == "true"  # the stored first response
    assert len(world.etsy.writes()) == 1
    assert [a.action for a in world.audits()] == ["action.succeeded"]
    assert CODE not in first.text
    assert CODE not in logs.raw()


@pytest.mark.ac("AC-6.6.2")
async def test_the_press__a_code_on_the_receipt_answers_already_present() -> None:
    world = World()
    shipment = OrderShipment(carrier="israel-post", tracking_code=CODE, notified_at=NOW)
    world.etsy.receipts[RECEIPT] = replace(paid_receipt(RECEIPT), shipments=(shipment,))
    async with client(world) as http:
        answer = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
    assert answer.status_code == 200
    assert (answer.json()["sent"], answer.json()["already_present"]) == (False, True)
    assert world.etsy.writes() == []


@pytest.mark.ac("AC-6.6.1")
@pytest.mark.ac("AC-6.6.3")
@pytest.mark.ac("AC-6.8.2")
@pytest.mark.parametrize(
    ("world", "role", "aal", "stepped_up", "problem"),
    [
        (World(), Role.ADMIN, "aal2", timedelta(minutes=1), "forbidden"),
        (World(), Role.OWNER, "aal1", None, "mfa-required"),
        (World(), Role.OWNER, "aal2", timedelta(minutes=11), "step-up-required"),
        (World(scopes=frozenset({"transactions_r"})), Role.OWNER, "aal2", timedelta(minutes=1),
         "scope-missing"),
        (World(flags=frozenset()), Role.OWNER, "aal2", timedelta(minutes=1), "write-flag-off"),
        (World(halted=True), Role.OWNER, "aal2", timedelta(minutes=1), "writes-halted"),
    ],
    ids=["admin", "no-mfa", "stale-step-up", "no-scope", "flag-off", "kill-switch"],
)  # fmt: skip
async def test_the_press__403_and_nothing_sent(
    world: World, role: Role, aal: str, stepped_up: timedelta | None, problem: str
) -> None:
    async with client(world, role, aal=aal, stepped_up=stepped_up) as http:
        answer = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
    assert answer.status_code == 403, answer.text
    assert answer.json()["type"] == PROBLEMS + problem
    if problem == "scope-missing":
        assert answer.json()["reconnect_url"] == "/v1/shops/etsy/connect"
        assert answer.json()["scope"] == "transactions_w"
    assert world.etsy.calls == []


async def test_the_press__needs_an_idempotency_key() -> None:
    world = World()
    async with client(world) as http:
        answer = await http.post(f"{BASE}/tracking-upload", json=PASTE)
        first = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
        other = await http.post(
            f"{BASE}/tracking-upload", json={**PASTE, "receipt_id": RECEIPT + 1},
            headers=press_headers(),
        )  # fmt: skip
    assert answer.status_code == 400
    assert answer.json()["type"] == PROBLEMS + "idempotency-key-required"
    assert first.status_code == 200
    assert other.status_code == 422
    assert other.json()["type"] == PROBLEMS + "idempotency-key-reused"
    assert len(world.etsy.writes()) == 1


async def test_a_refused_press__is_not_stored_so_the_same_key_works_after_the_step_up() -> None:
    world = World()
    async with client(world, stepped_up=timedelta(minutes=30)) as http:
        stale = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
    async with client(world) as http:
        done = await http.post(f"{BASE}/tracking-upload", json=PASTE, headers=press_headers())
    assert stale.json()["type"] == PROBLEMS + "step-up-required"
    assert done.status_code == 200, done.text
    assert len(world.etsy.writes()) == 1


async def test_vacation_mode__set_and_previewed() -> None:
    world = World()
    body: dict[str, Any] = {"is_vacation": True, "vacation_message": "Back on 2 November"}
    async with client(world) as http:
        preview = await http.post(f"{BASE}/vacation-mode/preview", json=body)
        done = await http.post(f"{BASE}/vacation-mode", json=body, headers=press_headers())
    assert preview.json()["target"] == body
    assert done.status_code == 200, done.text
    assert done.json()["outcome"] == "succeeded"
    assert world.etsy.on_vacation


def test_the_production_wiring__is_inert_until_the_owner_names_a_flag() -> None:
    """The composition root wires the writes for the Owner's press; with the default settings
    every write is off at the switch the gateway reads before each send."""
    from htco_api.main import owner_write_services  # noqa: PLC0415
    from htco_api.settings import ApiSettings, Environment  # noqa: PLC0415
    from htco_persistence.database import Database  # noqa: PLC0415
    from tests.application.test_etsy_owner_writes import FakeEtsy  # noqa: PLC0415

    settings = ApiSettings(environment=Environment.TEST)
    services = owner_write_services(
        settings,
        Database("postgresql://unused.invalid/none"),
        FakeEtsy(),  # type: ignore[arg-type]
    )
    gateway = services.gateway_for(Memberships)  # type: ignore[arg-type]

    assert settings.etsy_write_action_set == frozenset()
    for action in ("etsy.shipments.create", "etsy.shop.update"):
        assert not services.switches.is_on(action)
        assert not gateway._router.write_enabled(action)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="not an owner write"):
        ApiSettings(environment=Environment.TEST, etsy_write_actions="etsy.listings.update")


@pytest.mark.ac("AC-6.10.1")
async def test_a_relay_item__previewed_then_uploaded_and_closed() -> None:
    world = World()
    item = RelayItem(
        id=uuid.uuid7(), organization_id=ORG, shop_id=SHOP, receipt_id=RECEIPT,
        state=RelayState.HANDED_OFF, paid_at=NOW - timedelta(days=3), opened_at=NOW,
        updated_at=NOW, carrier="israel_post", last4="89IL",
    )  # fmt: skip
    world.relay.items[item.id] = item
    world.relay.codes[item.id] = CODE
    async with client(world) as http:
        preview = await http.get(f"/v1/relay-items/{item.id}/upload-preview")
        done = await http.post(f"/v1/relay-items/{item.id}/upload", headers=press_headers())
        again = await http.post(f"/v1/relay-items/{item.id}/upload", headers=press_headers("2"))
    assert preview.json()["target"]["last4"] == "89IL"
    assert done.status_code == 200, done.text
    assert done.json()["relay_item"]["closed_reason"] == "uploaded"
    assert CODE not in done.text
    assert CODE not in preview.text
    assert again.status_code == 409
    assert len(world.etsy.writes()) == 1


async def test_mark_shipped__previewed_and_pressed() -> None:
    world = World()
    async with client(world) as http:
        preview = await http.post(f"{BASE}/mark-shipped/preview", json={"receipt_id": RECEIPT})
        done = await http.post(
            f"{BASE}/mark-shipped", json={"receipt_id": RECEIPT}, headers=press_headers()
        )
    assert preview.json()["target"] == {"receipt_id": RECEIPT, "was_shipped": True}
    assert done.status_code == 200, done.text
    assert (done.json()["outcome"], done.json()["sent"]) == ("succeeded", True)
    assert world.etsy.receipts[RECEIPT].is_shipped


async def test_holiday_preference__listed_previewed_with_before_and_pressed() -> None:
    world = World()
    change = {"holiday_id": 7, "is_working": False}
    async with client(world) as http:
        listed = await http.get(f"{BASE}/holiday-preferences")
        preview = await http.post(f"{BASE}/holiday-preference/preview", json=change)
        done = await http.post(f"{BASE}/holiday-preference", json=change, headers=press_headers())
        after = await http.get(f"{BASE}/holiday-preferences")
    assert listed.status_code == 200, listed.text
    assert [h["holiday_id"] for h in listed.json()["items"]] == [7, 9]
    assert preview.json()["target"]["before"]["is_working"] is True
    assert done.status_code == 200, done.text
    assert done.json()["verified"] is True
    assert next(h for h in after.json()["items"] if h["holiday_id"] == 7)["is_working"] is False


@pytest.mark.parametrize(
    ("path", "body"),
    [("mark-shipped", {"receipt_id": RECEIPT}),
     ("holiday-preference", {"holiday_id": 7, "is_working": False})],
)  # fmt: skip
async def test_the_new_writes__flag_off_by_default_403(path: str, body: dict[str, Any]) -> None:
    world = World(flags=frozenset())
    async with client(world) as http:
        answer = await http.post(f"{BASE}/{path}", json=body, headers=press_headers())
    assert answer.status_code == 403
    assert answer.json()["type"] == PROBLEMS + "write-flag-off"
    assert world.etsy.writes() == []
