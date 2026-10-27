"""The committed OpenAPI document is the API contract (ER-ARCH-05, AC-1.3.1)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from htco_api.app import create_app
from tools.openapi import CONTRACT_PATH, contract_drift, render

REPO_ROOT = Path(__file__).resolve().parents[2]


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((REPO_ROOT / CONTRACT_PATH).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.ac("AC-1.3.1")
def test_contract__committed_file__matches_the_app() -> None:
    drift = contract_drift(create_app(), REPO_ROOT / CONTRACT_PATH)

    assert drift is None, drift


@pytest.mark.ac("AC-1.3.1")
def test_contract_drift__endpoint_changed_without_updating_the_file__is_reported(
    tmp_path: Path,
) -> None:
    committed = tmp_path / "openapi.yaml"
    committed.write_text(render(create_app()), encoding="utf-8")
    changed = create_app()

    async def version() -> dict[str, str]:
        return {"version": "1"}

    changed.add_api_route("/version", version)

    drift = contract_drift(changed, committed)

    assert drift is not None
    assert "just openapi" in drift


def test_contract_drift__missing_file__is_reported(tmp_path: Path) -> None:
    drift = contract_drift(create_app(), tmp_path / "openapi.yaml")

    assert drift is not None
    assert "just openapi" in drift


def test_contract__is_openapi_3_1_with_stable_operation_ids() -> None:
    contract = _contract()

    assert contract["openapi"].startswith("3.1.")
    operation_ids = {
        operation["operationId"]
        for path_item in contract["paths"].values()
        for operation in path_item.values()
    }
    assert operation_ids == {
        "healthz",
        "readyz",
        "me",
        "shops",
        "list_organization_members",
        "invite_organization_member",
        "change_member_role",
        "remove_organization_member",
        "grant_member_shop",
        "revoke_member_shop",
        "sign_in_web",
        "accept_web_invitation",
        "request_password_reset",
        "reset_password",
        "current_session",
        "sign_out_web",
        "get_mfa_status",
        "start_mfa_enrollment",
        "verify_mfa_enrollment",
        "confirm_step_up",
        "list_web_sessions",
        "revoke_web_session",
        "connect_etsy_shop",
        "complete_etsy_connection",
        "list_shop_integrations",
        "check_shop_capabilities",
        "disconnect_etsy_shop",
        "remove_etsy_shop",
        "get_shop_overview",
        "list_shop_orders",
        "get_shop_order",
        "export_shop_orders",
        "resync_shop_orders",
        "list_shop_listings",
        "list_shop_reviews",
        "get_shop_shipping",
        "get_shop_stats",
        "get_operations_health",
        "retry_dead_letter",
        "start_order_sync",
        "list_automations",
        "list_automation_executions",
        "start_automation_execution",
        "retry_automation_execution",
        "receive_etsy_webhook",
        "get_browser_environment",
        "get_browser_configuration",
        "put_browser_configuration",
        "get_execution_chain",
        "get_incidents",
        "get_incident",
        "move_incident",
        "get_notifications",
        "read_notifications",
        "get_relay_items",
        "start_passkey",
        "verify_passkey",
        "challenge_passkey",
        "confirm_with_passkey",
        "remove_factor",
    }


def test_contract__every_response__documents_the_request_id_header() -> None:
    responses = [
        response
        for path_item in _contract()["paths"].values()
        for operation in path_item.values()
        for response in operation["responses"].values()
    ]

    assert responses
    for response in responses:
        header = response["headers"]["X-Request-Id"]
        assert header["required"] is True
        assert header["schema"] == {"type": "string", "format": "uuid"}


def test_contract__error_responses__are_problem_json_with_a_schema() -> None:
    errors = [
        response
        for path_item in _contract()["paths"].values()
        for operation in path_item.values()
        for status, response in operation["responses"].items()
        if status.startswith(("4", "5"))
    ]

    assert errors
    for response in errors:
        assert list(response["content"]) == ["application/problem+json"]
        assert "$ref" in response["content"]["application/problem+json"]["schema"]


def test_contract__every_operation__documents_the_500_problem() -> None:
    """The middleware answers any unexpected error with a 500 problem, on every route."""
    contract = _contract()
    for path_item in contract["paths"].values():
        for operation in path_item.values():
            response = operation["responses"]["500"]
            schema = response["content"]["application/problem+json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/Problem"}


def test_render__is_deterministic() -> None:
    assert render(create_app()) == render(create_app())
