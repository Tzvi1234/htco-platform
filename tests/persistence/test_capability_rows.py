"""The shop card's probe results arrive aggregated with their connection, in one statement."""

from datetime import UTC, datetime

from htco_domain.integrations import CapabilityState, ShopCapability
from htco_persistence.shop_integrations import _LIST_INTEGRATIONS, _capabilities


def test_list_integrations__reads_capabilities_in_the_same_statement() -> None:
    """One statement is one snapshot: a reconnect cannot slip between two reads (Codex, #49)."""
    assert _LIST_INTEGRATIONS.count("select") == 2  # the outer query and its subquery
    assert "app.shop_capabilities" in _LIST_INTEGRATIONS


def test_capabilities__parsed_from_json_in_display_order() -> None:
    rows = [
        ["shipment_write", "unknown", "no_safe_probe", "2026-10-16T09:00:00.5+00:00"],
        ["shop_details", "available", "read_ok", "2026-10-16T09:00:00+00:00"],
    ]

    found = _capabilities(rows)

    assert [(r.capability, r.state, r.detail) for r in found] == [
        (ShopCapability.SHOP_DETAILS, CapabilityState.AVAILABLE, "read_ok"),
        (ShopCapability.SHIPMENT_WRITE, CapabilityState.UNKNOWN, "no_safe_probe"),
    ]
    assert found[0].checked_at == datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def test_capabilities__none_yet() -> None:
    assert _capabilities([]) == ()
