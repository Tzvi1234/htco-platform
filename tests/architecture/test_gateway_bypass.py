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

import ast
from enum import StrEnum
from pathlib import Path

import pytest

from tests.architecture.gateway_bypass import (
    ADAPTER_MODULE,
    DYNAMIC_GETATTR,
    Call,
    calls_of,
    channel_runs,
    choke_point_uses,
    etsy_operations,
    etsy_urls,
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
        REPO_ROOT / "tools",
    ]
)
APPLICATION = "packages/application/src/htco_application/"
INTEGRATIONS = "packages/integrations/src/htco_integrations/"
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
        # The owner's writes (6.6, 6.8): re-read before, send once, re-read to verify.
        ("execution/channels.py", "EtsyApiChannel._write", "get_order_receipt"),
        ("execution/channels.py", "EtsyApiChannel._write", "create_receipt_shipment"),
        ("execution/channels.py", "EtsyApiChannel._write", "get_shop"),
        ("execution/channels.py", "EtsyApiChannel._write", "update_shop_vacation"),
        ("execution/channels.py", "EtsyApiChannel.verify", "get_order_receipt"),
        ("execution/channels.py", "EtsyApiChannel.verify", "get_shop"),
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


def test_the_write_choke_point_is_pinned() -> None:
    """Only `_request` calls `_send`, and only `_send` and the client hook read the per-request
    write flag: no other path in the adapter can carry a write past both guards."""
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    send_callers = {where for where, name in _named(tree) if name == "_send"}
    flag_users = {where for where, name in _named(tree) if name == "_WRITE_ALLOWED"}

    assert send_callers == {"EtsyAdapter._request"}
    assert flag_users == {"<module>", "EtsyAdapter._send", "_WriteGuard.__call__"}


# Etsy URLs named outside the adapter, exactly: the OAuth client's two endpoints (consent in the
# browser, the token exchange) and the documentation it cites. Anything else is a second client.
ETSY_URLS_OUTSIDE_THE_ADAPTER = frozenset(
    {
        (APPLICATION + "etsy_connect.py", "developers.etsy.com"),
        (
            INTEGRATIONS + "etsy_oauth.py",
            "developers.etsy.com/documentation/essentials/authentication",
        ),
        (INTEGRATIONS + "etsy_oauth.py", "www.etsy.com/oauth/connect"),
        (INTEGRATIONS + "etsy_oauth.py", "api.etsy.com/v3/public/oauth/token"),
    }
)


def test_only_the_adapter_names_etsys_api_hosts() -> None:
    """A second Etsy client would escape every rule above, which key on the adapter: any Etsy
    host (openapi.etsy.com, api.etsy.com, *.etsy.com) outside it is on the exact list above."""
    named = {
        (_relative(path), url.rstrip(".,;"))
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
        if path.resolve() != ADAPTER.resolve()
        for url in etsy_urls(path.read_text(encoding="utf-8"))
    }

    assert named == ETSY_URLS_OUTSIDE_THE_ADAPTER


def test_scanner__finds_every_etsy_host() -> None:
    text = (
        "A = 'https://api.etsy.com/v3/application/shops'\n"
        'B = "https://openapi.etsy.com/v3/application"\n'
        "C = f'https://{host}.etsy.com/x'  # an unseen subdomain\n"
        "D = 'https://ETSY.COM/x'\n"
        "E = 'https://notetsy.company.example/x'\n"
    )

    assert etsy_urls(text) == [
        "api.etsy.com/v3/application/shops",
        "openapi.etsy.com/v3/application",
        ".etsy.com/x",
        "etsy.com/x",
    ]


def test_nothing_outside_the_adapter_reaches_its_choke_point() -> None:
    """Sage and Codex on #106: outside etsy.py, nothing names `_request`, `_send` or the write
    flag, passes `allow_write=`, or spells the flag's request extension; a call such as
    `adapter._request('POST', ..., allow_write=bool(1))` would pass the runtime guard."""
    uses = [
        (_relative(path), where, what)
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
        if path.resolve() != ADAPTER.resolve()
        for where, what in choke_point_uses(_parse(path))
    ]

    assert uses == []


def test_scanner__finds_every_way_to_the_choke_point_from_outside(tmp_path: Path) -> None:
    module = tmp_path / "sneaky.py"
    module.write_text(
        "async def by_flag(adapter):\n"
        "    await adapter._request('POST', 'op', 1, '/shops/1', {}, allow_write=bool(1))\n"
        "async def by_name(adapter):\n"
        "    await getattr(adapter, '_request')('POST', 'op', 1, '/shops/1', {})\n"
        "async def by_extension(http, base):\n"
        "    await http.post(base + '/shops/1', extensions={'htco.etsy.write_allowed': True})\n"
        "async def fine(store):\n"
        "    await store.request(1)\n",
        encoding="utf-8",
    )

    assert sorted(choke_point_uses(_parse(module))) == [
        ("by_extension", "htco.etsy.write_allowed"),
        ("by_flag", "_request"),
        ("by_flag", "allow_write="),
        ("by_name", "_request"),
    ]


def test_only_the_adapter_calls_its_request_choke_point() -> None:
    """A second class in etsy.py calling `adapter._request(..., allow_write=True)` would escape
    `etsy_writes`, which reads the adapter's methods and the module's functions only."""
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    users = {where for where, name in _named(tree) if name == "_request"}

    assert users
    assert all(where.startswith("EtsyAdapter.") for where in users), users


# Sage (a): the API channel holds the write port; only the gateway runs a channel.
CHANNEL_HOLDERS = frozenset({API_CHANNEL, "workers/runner/src/htco_worker/main.py"})
CHANNEL_RUNNERS = frozenset({APPLICATION + "execution/service.py"})


def test_only_the_composition_roots_hold_the_etsy_api_channel() -> None:
    holders = {
        _relative(path)
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
        if any(name == "EtsyApiChannel" for _, name in _named(_parse(path)))
    }

    # The class is defined in the channel module; the worker builds the only one today.
    assert holders <= CHANNEL_HOLDERS | {"apps/api/src/htco_api/main.py"}, holders
    assert "workers/runner/src/htco_worker/main.py" in holders


def test_only_the_gateway_runs_a_channel() -> None:
    """`adapter.execute(request, binding)` sends the action; outside the gateway's service it
    would skip every check."""
    runners = {
        _relative(path)
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
        if channel_runs(_parse(path))
    }

    assert runners == CHANNEL_RUNNERS


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _named(tree: ast.AST) -> list[tuple[str, str]]:
    """(the qualified function, the name) of every name and attribute in `tree`."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = (*scope, child.name)
            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "getattr"
                and len(child.args) >= 2  # getattr(object, "name")
                and isinstance(child.args[1], ast.Constant)
                and isinstance(child.args[1].value, str)
            ):
                found.append((".".join(scope) or "<module>", child.args[1].value))
            elif isinstance(child, ast.Name):
                found.append((".".join(scope) or "<module>", child.id))
            elif isinstance(child, ast.Attribute):
                found.append((".".join(scope) or "<module>", child.attr))
            visit(child, inner)

    visit(tree, ())
    return found


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


def test_scanner__any_write_flag_but_a_literal_false__is_a_write(tmp_path: Path) -> None:
    adapter = tmp_path / "etsy.py"
    adapter.write_text(
        "class EtsyAdapter:\n"
        "    async def by_flag(self, flag):\n"
        "        return await self._request('POST', 1, allow_write=flag)\n"
        "    async def by_options(self, **options):\n"
        "        return await self._request('POST', 1, **options)\n"
        "    async def by_send(self):\n"
        "        return await self._send('POST', 1, write=True)\n"
        "    async def says_no(self):\n"
        "        return await self._request('GET', 1, allow_write=False)\n"
        "    async def _request(self, method, shop_id, *, allow_write=False):\n"
        "        return await self._send(method, shop_id, write=allow_write)\n"
        "    async def _send(self, method, shop_id, *, write):\n"
        "        return None\n",
        encoding="utf-8",
    )

    # `_request` and `_send` are the choke point every call passes (pinned on the real adapter
    # by test_the_write_choke_point_is_pinned): what a caller hands them decides a write.
    assert etsy_writes(adapter) == {"by_flag", "by_options", "by_send"}


def test_scanner__a_plain_method_that_reaches_etsy_is_an_operation(tmp_path: Path) -> None:
    """Sage's note on #106: a public `def` (not a coroutine) that reaches the choke point, here
    through a private helper, is an Etsy operation too; a plain accessor is not."""
    adapter = tmp_path / "etsy.py"
    adapter.write_text(
        "class EtsyAdapter:\n"
        "    def start_read(self, shop_id):\n"
        "        return self._later(shop_id)\n"
        "    def config(self):\n"
        "        return self._config\n"
        "    def _later(self, shop_id):\n"
        "        return self._request('GET', shop_id)\n"
        "    async def get_shop(self, shop_id):\n"
        "        return await self._request('GET', shop_id)\n"
        "    async def _request(self, method, shop_id, *, allow_write=False):\n"
        "        return None\n",
        encoding="utf-8",
    )

    assert etsy_operations(adapter) == {"start_read", "get_shop"}


def test_scanner__getattr_with_a_written_name_is_that_name(tmp_path: Path) -> None:
    adapter = tmp_path / "etsy.py"
    adapter.write_text(
        "class EtsyAdapter:\n"
        "    async def by_getattr(self):\n"
        "        return await getattr(self, '_send')('POST', 1, write=True)\n"
        "    async def by_dynamic(self, name):\n"
        "        return await getattr(self, name)('POST', 1)\n"
        "    async def reads(self):\n"
        "        return await getattr(self, '_request')('GET', 1)\n"
        "    async def _request(self, method, shop_id, *, allow_write=False):\n"
        "        return await self._send(method, shop_id, write=allow_write)\n"
        "    async def _send(self, method, shop_id, *, write):\n"
        "        return None\n",
        encoding="utf-8",
    )

    # A dynamic getattr inside the adapter could reach the choke point: it fails closed.
    assert etsy_writes(adapter) == {"by_getattr", "by_dynamic"}
    assert ("EtsyAdapter.by_getattr", "_send") in _named(ast.parse(adapter.read_text("utf-8")))


def test_scanner__every_reference_to_an_operation__is_a_site(sources: Path) -> None:
    adapter = sources / "etsy.py"
    module = sources / "app" / "refs.py"
    module.parent.mkdir()
    module.write_text(
        "import functools\n"
        "async def alias(etsy):\n"
        "    send = etsy.create_shipment\n"
        "    await send(1, 'LX')\n"
        "async def callback(etsy, gateway):\n"
        "    await gateway.retry(etsy.create_shipment, 1)\n"
        "async def by_name(etsy):\n"
        "    await getattr(etsy, 'create_shipment')(1, 'LX')\n"
        "async def bound(etsy):\n"
        "    await functools.partial(etsy.create_shipment, 1)('LX')\n"
        "async def dynamic(etsy, name):\n"
        "    await getattr(etsy, name)(1)\n"
        "class Fields:\n"
        "    def check(self, name):\n"
        "        return getattr(self, name)\n",
        encoding="utf-8",
    )

    calls = calls_of([sources], etsy_writes(adapter), skip=adapter)

    # A getattr with a name not known in the source fails closed; one on `self` reads the
    # object's own fields and is not an Etsy reference.
    assert [(c.where, c.method) for c in calls] == [
        ("alias", "create_shipment"),
        ("callback", "create_shipment"),
        ("by_name", "create_shipment"),
        ("bound", "create_shipment"),
        ("dynamic", DYNAMIC_GETATTR),
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
