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
MODULES: dict[str, tuple[Route, str]] = {
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
# Every call site, exactly: (module, the function it is in, the operation). A new call, even in
# a module above, fails until it is named here (Codex review of #106).
CALL_SITES: frozenset[tuple[str, str, str]] = frozenset(
    (APPLICATION + module, where, operation)
    for module, where, operation in [
        ("etsy_connect.py", "complete_connection", "shop_of_user"),
        ("etsy_webhooks.py", "WebhookProcessor._reread", "get_listing"),
        ("etsy_webhooks.py", "WebhookProcessor._reread", "get_listing_inventory"),
        ("execution/channels.py", "EtsyApiChannel._order_history", "get_order_receipt"),
        ("execution/channels.py", "EtsyApiChannel._order_history", "list_order_receipts"),
        ("execution/channels.py", "EtsyApiChannel._receipts", "get_receipt"),
        ("execution/channels.py", "EtsyApiChannel._receipts", "list_receipts"),
        ("execution/channels.py", "EtsyApiChannel.execute", "get_shop"),
        ("order_automations.py", "OrderVerification.__call__", "get_order_receipt"),
        ("order_history.py", "OrderSync._read", "list_order_receipts"),
        ("order_intake.py", "OrderIntake._count", "list_order_receipts"),
        ("order_intake.py", "OrderIntake._page", "list_order_receipts"),
        ("order_intake.py", "OrderIntake._receipt", "get_order_receipt"),
        ("shop_capabilities.py", "_probe", "check_receipt_addresses"),
        ("shop_capabilities.py", "_probe", "get_shop"),
        ("shop_capabilities.py", "_probe", "list_listings"),
        ("shop_dashboard.py", "_Opened._read_receipts", "list_order_receipts"),
        ("shop_dashboard.py", "_Opened.listings", "list_listing_details"),
        ("shop_dashboard.py", "_Opened.listings", "list_sections"),
        ("shop_dashboard.py", "_Opened.next_receipt.load", "list_order_receipts"),
        ("shop_dashboard.py", "_Opened.profile", "get_shop_profile"),
        ("shop_dashboard.py", "_Opened.reviews", "list_reviews"),
        ("shop_dashboard.py", "_Opened.shipping", "list_production_partners"),
        ("shop_dashboard.py", "_Opened.shipping", "list_return_policies"),
        ("shop_dashboard.py", "_Opened.shipping", "list_shipping_profiles"),
        ("tracking_sync.py", "TrackingSync.__call__", "get_order_receipt"),
    ]
)
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
    return "\n".join(
        f"  {_relative(c.path)}:{c.line} ({c.where}) calls {c.method}()" for c in calls
    )


def _site(call: Call) -> tuple[str, str, str]:
    return _relative(call.path), call.where, call.method


def test_only_the_composition_roots_import_the_etsy_adapter() -> None:
    importers = {_relative(path) for path in importers_of(SOURCE_ROOTS, ADAPTER_MODULE)}

    assert importers - {_relative(ADAPTER)} == ADAPTER_IMPORTERS


def test_every_direct_etsy_call_is_on_the_documented_list() -> None:
    operations = etsy_operations(ADAPTER)
    assert {"get_order_receipt", "list_reviews", "shop_of_user"} <= operations  # the scan works

    calls = calls_of(SOURCE_ROOTS, operations, skip=ADAPTER)
    sites = {_site(call) for call in calls}

    undocumented = [call for call in calls if _site(call) not in CALL_SITES]
    assert undocumented == [], "Etsy called outside the documented list:\n" + _describe(
        undocumented
    )
    gone = sorted(CALL_SITES - sites)
    assert gone == [], f"documented but no longer calling Etsy: {gone}"
    assert {module for module, _, _ in CALL_SITES} == set(MODULES)  # each one has its reason


def test_an_etsy_write_is_sent_only_from_the_gateway_api_channel() -> None:
    writes = etsy_writes(ADAPTER)

    calls = calls_of(SOURCE_ROOTS, writes, skip=ADAPTER) if writes else []
    outside = [call for call in calls if _relative(call.path) != API_CHANNEL]

    assert outside == [], "Etsy write sent outside the gateway's API channel:\n" + _describe(
        outside
    )


def test_no_read_bypass_is_the_api_channel() -> None:
    assert MODULES[API_CHANNEL][0] is Route.GATEWAY
    assert all(reason for _, reason in MODULES.values())


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
        Call(module, 2, "get_shop", "run"),
        Call(module, 3, "create_shipment", "run"),
    ]


def test_scanner__a_write_delegated_to_a_private_helper__is_a_write(tmp_path: Path) -> None:
    adapter = tmp_path / "etsy.py"
    adapter.write_text(
        "def _form(code):\n"
        "    return {'tracking_code': code}\n"
        "async def _module_send(adapter, path):\n"
        "    return await adapter._request('PUT', path, allow_write=True)\n"
        "class EtsyAdapter:\n"
        "    async def create_shipment(self, shop_id, code):\n"
        "        return await self._write(shop_id, _form(code))\n"
        "    async def set_vacation(self, shop_id):\n"
        "        return await _module_send(self, '/shops')\n"
        "    async def get_shop(self, shop_id):\n"
        "        return await self._request('GET', shop_id)\n"
        "    async def _write(self, shop_id, form):\n"
        "        return await self._post(shop_id, form)\n"
        "    async def _post(self, shop_id, form):\n"
        "        return await self._request('POST', shop_id, allow_write=True)\n"
        "    async def _request(self, method, shop_id, *, allow_write=False):\n"
        "        return None\n",
        encoding="utf-8",
    )

    # Every name that reaches a write, private helpers included: a call of any of them
    # outside the API channel is a write sent outside the gateway.
    assert etsy_writes(adapter) == {
        "create_shipment",
        "set_vacation",
        "_write",
        "_post",
        "_module_send",
    }


def test_scanner__names_the_function_each_call_is_in(sources: Path) -> None:
    adapter = sources / "etsy.py"
    module = sources / "app" / "shop.py"
    module.parent.mkdir()
    module.write_text(
        "class Dashboard:\n"
        "    async def profile(self, etsy):\n"
        "        return await etsy.get_shop(1)\n"
        "    async def extra(self, etsy):\n"
        "        return await etsy.get_shop(2)\n"
        "async def loose(etsy):\n"
        "    return await etsy.get_shop(3)\n",
        encoding="utf-8",
    )

    calls = calls_of([sources], etsy_operations(adapter), skip=adapter)

    assert [(c.where, c.method) for c in calls] == [
        ("Dashboard.profile", "get_shop"),
        ("Dashboard.extra", "get_shop"),
        ("loose", "get_shop"),
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
