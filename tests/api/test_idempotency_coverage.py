"""Every unsafe route requires an `Idempotency-Key` or is exempt for a stated reason
(architecture audit 6.1; SOFTWARE_DESIGN: unsafe `POST` endpoints that create or trigger work).

A new `POST`, `PUT`, `PATCH` or `DELETE` fails here until it either requires the header (through
`run_idempotently`) or is added to `EXEMPT` with why a retry is safe without it. The four
routes of audit 6.1 are staged: they declare the key and replay on it, and require it once
`STAGED_KEY_REQUIRED` flips.
"""

from typing import Any

from htco_api.app import create_app
from htco_api.idempotency import STAGED_KEY_REQUIRED

UNSAFE = frozenset({"post", "put", "patch", "delete"})
_SESSION = (
    "a sign-in or session flow: bound to the session cookie and a one-time challenge or token "
    "that the sign-in service accepts once; its answers carry secrets, never stored for replay"
)

# operationId -> why a client retry without the header does no harm.
EXEMPT: dict[str, str] = {
    "change_member_role": "PATCH sets the role: repeating it changes nothing",
    "remove_organization_member": "DELETE: a repeat answers 404 and changes nothing",
    "grant_member_shop": "PUT: granting an existing grant changes nothing",
    "revoke_member_shop": "DELETE: a repeat answers 404 and changes nothing",
    "sign_in_web": _SESSION,
    "accept_web_invitation": _SESSION,
    "request_password_reset": _SESSION,
    "reset_password": _SESSION,
    "sign_out_web": _SESSION,
    "start_mfa_enrollment": _SESSION,
    "verify_mfa_enrollment": _SESSION,
    "confirm_step_up": _SESSION,
    "revoke_web_session": _SESSION,
    "start_passkey": _SESSION,
    "verify_passkey": _SESSION,
    "challenge_passkey": _SESSION,
    "confirm_with_passkey": _SESSION,
    "remove_factor": _SESSION,
    "connect_etsy_shop": "starts an OAuth flow with a fresh state each time; creates nothing",
    "complete_etsy_connection": "the OAuth state is taken once; a repeat is refused",
    "check_shop_capabilities": "read-only probes, reserved per shop (a repeat is rate-limited)",
    "disconnect_etsy_shop": "a disconnected shop stays disconnected; a repeat changes nothing",
    "remove_etsy_shop": "DELETE: a repeat answers 404 and changes nothing",
    "export_shop_orders": "a read: the CSV holds buyer data and is never stored for replay",
    "start_order_sync": "at most one running sync per shop (its natural key); reads only",
    "retry_automation_execution": "only a dead-lettered execution retries; a repeat is a 409 "
    "(state check); the queue's own retry is idempotent",
    "receive_etsy_webhook": "Etsy's webhook ID deduplicates in the inbox; Etsy sends no key",
    "move_incident": "a state transition: a repeat is refused (409)",
    "read_notifications": "marking read again changes nothing",
}


def _operations() -> dict[str, dict[str, Any]]:
    schema = create_app().openapi()
    return {
        operation["operationId"]: operation
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in UNSAFE
    }


# Audit 6.1, staged (STAGED_KEY_REQUIRED): they replay on the key now and require it once the web
# client sends it; the contract check refuses a new required header as a breaking change.
STAGED = frozenset(
    {"invite_organization_member", "resync_shop_orders", "retry_dead_letter", "paste_tracking"}
)


def _requires_the_key(operation: dict[str, Any]) -> bool:
    return any(
        parameter.get("in") == "header"
        and parameter.get("name") == "Idempotency-Key"
        and (
            parameter.get("required") is True
            or (operation["operationId"] in STAGED and parameter.get("required") is False)
        )
        for parameter in operation.get("parameters", [])
    )


def test_the_staged_routes__declare_the_key_as_the_switch_says() -> None:
    operations = _operations()
    for name in STAGED:
        [parameter] = [
            p for p in operations[name]["parameters"] if p.get("name") == "Idempotency-Key"
        ]
        assert parameter["required"] is STAGED_KEY_REQUIRED, name


def test_every_unsafe_route__requires_an_idempotency_key_or_is_exempt_with_a_reason() -> None:
    operations = _operations()
    idempotent = {name for name, op in operations.items() if _requires_the_key(op)}

    unaccounted = sorted(set(operations) - idempotent - EXEMPT.keys())
    assert unaccounted == [], f"require Idempotency-Key or add to EXEMPT: {unaccounted}"
    assert sorted(idempotent & EXEMPT.keys()) == []
    assert sorted(EXEMPT.keys() - operations.keys()) == [], "EXEMPT names routes that are gone"
    assert all(reason.strip() for reason in EXEMPT.values())


def test_the_routes_that_trigger_work__declare_the_key() -> None:
    """The four of audit 6.1 (staged: declared, optional until STAGED_KEY_REQUIRED flips), and
    the automation trigger of slice 5.2."""
    operations = _operations()
    assert {
        "invite_organization_member",
        "resync_shop_orders",
        "retry_dead_letter",
        "paste_tracking",
        "start_automation_execution",
    } <= {name for name, op in operations.items() if _requires_the_key(op)}
