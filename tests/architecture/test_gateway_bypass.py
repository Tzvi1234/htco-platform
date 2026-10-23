"""Architecture contract: Etsy is reached through the Action Gateway (audit P1-1, ADR-0019).

Writes are safe today because the adapter is GET-only (`test_etsy_read_only.py`); the first write
tool (ADR-0022, slices 6.6 and 6.8) removes that guard for one operation at a time. Before it does,
this contract holds three things:

- only the composition roots (and the OAuth client) import the Etsy adapter;
- every direct call of an adapter operation is on the documented list below: either the gateway
  path, or a named read bypass with its reason and its way out;
- an operation that sends a write is called only from the gateway's API channel.

The list is exact: a new direct call fails until it is documented here, and a bypass that is
removed fails until it is struck from the list, so the list never drifts from the code.
"""

from enum import StrEnum
from pathlib import Path

import pytest

from tests.architecture.gateway_bypass import (
    ADAPTER_MODULE,
    Call,
    calls_of,
    etsy_operations,
    etsy_writes,
    importers_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / "packages" / "integrations" / "src" / "htco_integrations" / "etsy.py"
SOURCE_ROOTS = sorted(
    [
        *REPO_ROOT.glob("packages/*/src"),
        *REPO_ROOT.glob("apps/*/src"),
        *REPO_ROOT.glob("workers/*/src"),
    ]
)
APPLICATION = "packages/application/src/htco_application/"
# The gateway's API channel: the one place an Etsy write may be sent from.
API_CHANNEL = APPLICATION + "execution/channels.py"


class Route(StrEnum):
    GATEWAY = "gateway"
    READ_BYPASS = "read bypass"


# Module → (path, why). Read bypasses name their way out; none of them may write.
DIRECT_CALLS: dict[str, tuple[Route, str]] = {
    API_CHANNEL: (Route.GATEWAY, "the gateway's API channel itself"),
    APPLICATION + "order_intake.py": (
        Route.GATEWAY,
        "GatewayReceipts: the order history's receipt source through `etsy.receipts.read`",
    ),
    APPLICATION + "order_automations.py": (Route.GATEWAY, "GatewayReceipts, per execution"),
    APPLICATION + "tracking_sync.py": (Route.GATEWAY, "GatewayReceipts, per execution"),
    APPLICATION + "order_history.py": (
        Route.READ_BYPASS,
        "the API-process sync, the fallback behind HTCO_ORDER_SYNC_IN_API (phase-05 5.4)",
    ),
    APPLICATION + "shop_capabilities.py": (
        Route.READ_BYPASS,
        "the capability probes (slice 4.5), maintenance reads outside any automation",
    ),
    APPLICATION + "shop_dashboard.py": (
        Route.READ_BYPASS,
        "the dashboard's live reads for a signed-in member (Phase 4); no automation runs them",
    ),
    APPLICATION + "etsy_webhooks.py": (
        Route.READ_BYPASS,
        "the listing and inventory re-reads after a webhook; to move onto read actions (P1-1)",
    ),
    APPLICATION + "etsy_connect.py": (
        Route.READ_BYPASS,
        "`shop_of_user` while connecting, before the shop exists for the gateway to resolve",
    ),
}
# Only these import the adapter: they build it and hand it to the parts above.
ADAPTER_IMPORTERS = frozenset(
    {
        "apps/api/src/htco_api/main.py",
        "workers/runner/src/htco_worker/main.py",
        "packages/integrations/src/htco_integrations/etsy_oauth.py",
    }
)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _describe(calls: list[Call]) -> str:
    return "\n".join(f"  {_relative(c.path)}:{c.line} calls {c.method}()" for c in calls)


def test_only_the_composition_roots_import_the_etsy_adapter() -> None:
    importers = {_relative(path) for path in importers_of(SOURCE_ROOTS, ADAPTER_MODULE)}

    assert importers - {_relative(ADAPTER)} == ADAPTER_IMPORTERS


def test_every_direct_etsy_call_is_on_the_documented_list() -> None:
    operations = etsy_operations(ADAPTER)
    assert {"get_order_receipt", "list_reviews", "shop_of_user"} <= operations  # the scan works

    calls = calls_of(SOURCE_ROOTS, operations, skip=ADAPTER)
    modules = {_relative(call.path) for call in calls}

    undocumented = [call for call in calls if _relative(call.path) not in DIRECT_CALLS]
    assert undocumented == [], "Etsy called outside the documented list:\n" + _describe(
        undocumented
    )
    assert modules == set(DIRECT_CALLS), "documented but no longer calling Etsy: " + ", ".join(
        sorted(set(DIRECT_CALLS) - modules)
    )


def test_an_etsy_write_is_sent_only_from_the_gateway_api_channel() -> None:
    writes = etsy_writes(ADAPTER)

    calls = calls_of(SOURCE_ROOTS, writes, skip=ADAPTER) if writes else []
    outside = [call for call in calls if _relative(call.path) != API_CHANNEL]

    assert outside == [], "Etsy write sent outside the gateway's API channel:\n" + _describe(
        outside
    )


def test_no_read_bypass_is_the_api_channel() -> None:
    assert DIRECT_CALLS[API_CHANNEL][0] is Route.GATEWAY
    assert all(reason for _, reason in DIRECT_CALLS.values())


# --- The scanner itself ----------------------------------------------------------------------


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    adapter = tmp_path / "etsy.py"
    adapter.write_text(
        "class EtsyAdapter:\n"
        "    async def get_shop(self, shop_id):\n"
        "        return await self._request('GET', shop_id)\n"
        "    async def create_shipment(self, shop_id, tracking):\n"
        "        return await self._request('POST', shop_id, allow_write=True)\n"
        "    async def _request(self, method, shop_id, *, allow_write=False):\n"
        "        return None\n",
        encoding="utf-8",
    )
    return tmp_path


def test_scanner__finds_the_operations_and_the_writes(sources: Path) -> None:
    adapter = sources / "etsy.py"

    assert etsy_operations(adapter) == {"get_shop", "create_shipment"}
    assert etsy_writes(adapter) == {"create_shipment"}


def test_scanner__reports_a_new_direct_call_and_a_write_call(sources: Path) -> None:
    adapter = sources / "etsy.py"
    module = sources / "app" / "fulfil.py"
    module.parent.mkdir()
    module.write_text(
        "async def run(etsy):\n"
        "    await etsy.get_shop(1)\n"
        "    await etsy.create_shipment(1, 'LX')\n",
        encoding="utf-8",
    )

    calls = calls_of([sources], etsy_operations(adapter), skip=adapter)

    assert calls == [
        Call(module, 2, "get_shop"),
        Call(module, 3, "create_shipment"),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "import htco_integrations.etsy",
        "from htco_integrations.etsy import EtsyAdapter",
        "from htco_integrations import etsy",
    ],
)
def test_scanner__reports_every_way_to_import_the_adapter(tmp_path: Path, statement: str) -> None:
    module = tmp_path / "service.py"
    module.write_text(statement + "\n", encoding="utf-8")
    other = tmp_path / "email.py"
    other.write_text("from htco_integrations import email\n", encoding="utf-8")

    assert importers_of([tmp_path], ADAPTER_MODULE) == [module]
