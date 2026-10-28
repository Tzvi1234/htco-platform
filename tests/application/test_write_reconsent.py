"""The re-consent of an owner write's scope (AC-6.8.2): the Etsy connect flow again, asking for
the read scopes plus exactly the one write scope, bound to the Owner's session and audited."""

from urllib.parse import parse_qs, urlsplit

import pytest

from htco_application.etsy_connect import (
    ETSY_CONNECT_SCOPES,
    StateRejectedError,
    UnknownWriteScopeError,
    start_connection,
    start_write_reconsent,
)
from tests.application.test_etsy_connect import (
    ORG,
    OTHER_SESSION,
    OWNER,
    SESSION,
    World,
    owner,
)
from tests.etsy_fakes import ETSY_SHOP, NOW

pytestmark = pytest.mark.anyio

SHOP = ORG  # any identifier: the audit names it


async def reconsent(
    world: World, scope: str = "transactions_w", held: frozenset[str] = frozenset()
) -> str:
    return await start_write_reconsent(
        world.etsy, organization_id=ORG, shop_id=SHOP, user_id=OWNER, session_id=SESSION,
        write_scope=scope, reason="tracking_upload", now=NOW, correlation_id="req-1",
        held_scopes=held,
    )  # fmt: skip


def asked(url: str) -> set[str]:
    return set(parse_qs(urlsplit(url).query)["scope"][0].split(" "))


@pytest.mark.ac("AC-6.8.2")
@pytest.mark.parametrize("scope", ["transactions_w", "shops_w"])
async def test_the_reconsent__asks_for_exactly_the_read_scopes_plus_one_write(scope: str) -> None:
    world = World()

    url = await reconsent(world, scope)

    assert asked(url) == ETSY_CONNECT_SCOPES | {scope}
    [pending] = world.pending.rows.values()
    assert pending.scopes == ETSY_CONNECT_SCOPES | {scope}


async def test_the_normal_connect__stays_read_only() -> None:
    world = World()

    url = await start_connection(
        world.etsy, organization_id=ORG, user_id=OWNER, session_id=SESSION, now=NOW
    )

    assert asked(url) == ETSY_CONNECT_SCOPES
    assert not any(s.endswith("_w") for s in asked(url))


async def test_the_reconsent__is_bound_to_the_owners_session_and_audited() -> None:
    world = World()
    url = await reconsent(world)
    [state] = parse_qs(urlsplit(url).query)["state"]

    [pending] = world.pending.rows.values()
    assert (pending.organization_id, pending.user_id, pending.session_id) == (ORG, OWNER, SESSION)
    [record] = world.audit.records
    assert record.action == "shop.write_reconsent_started"
    assert record.after == {"scope": "transactions_w", "write": "tracking_upload", "kept": []}
    assert state not in repr(record)
    # Another session cannot finish it (the connect flow's own check, AC-4.3.1).
    with pytest.raises(StateRejectedError):
        await world.complete(state, session=OTHER_SESSION)


async def test_the_callback__stores_the_write_scope_on_the_grant() -> None:
    world = World()
    [state] = parse_qs(urlsplit(await reconsent(world)).query)["state"]

    connected = await world.complete(state, auth=owner())

    assert connected.integration.etsy_shop_id == ETSY_SHOP
    assert "transactions_w" in connected.integration.scopes
    assert world.connector.requests[0].scopes == ETSY_CONNECT_SCOPES | {"transactions_w"}


@pytest.mark.parametrize("scope", ["listings_w", "email_r", "transactions_r"])
async def test_only_an_owner_writes_scope__may_be_asked_for(scope: str) -> None:
    world = World()

    with pytest.raises(UnknownWriteScopeError):
        await reconsent(world, scope)

    assert world.pending.rows == {}
    assert world.audit.records == []


@pytest.mark.ac("AC-6.8.2")
async def test_a_reconsent__keeps_the_write_scope_the_shop_holds() -> None:
    """Etsy's new grant replaces the old one: holding transactions_w, a re-consent for shops_w
    asks for both, and the stored grant holds both. Never another scope than the writes'."""
    world = World()
    held = ETSY_CONNECT_SCOPES | {"transactions_w", "email_r"}

    url = await reconsent(world, "shops_w", held)
    [state] = parse_qs(urlsplit(url).query)["state"]
    connected = await world.complete(state, auth=owner())

    both = ETSY_CONNECT_SCOPES | {"transactions_w", "shops_w"}
    assert asked(url) == both  # email_r is no owner write's: never asked for here
    assert connected.integration.scopes == both
    [record] = [r for r in world.audit.records if r.action == "shop.write_reconsent_started"]
    assert record.after == {"scope": "shops_w", "write": "tracking_upload",
                            "kept": ["transactions_w"]}  # fmt: skip
