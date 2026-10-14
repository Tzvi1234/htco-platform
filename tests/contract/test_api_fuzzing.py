"""Property-based checks of the running app against the committed contract (AC-1.3.3).

Schemathesis generates requests from `packages/contracts/openapi.yaml`, including methods the
contract does not allow; every response must match the contract and none may be a server error.
"""

from pathlib import Path
from typing import Any

import pytest
import schemathesis
from fastapi.testclient import TestClient
from hypothesis import settings

from htco_api.app import create_app
from tools.openapi import CONTRACT_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://api.test"

schema = schemathesis.openapi.from_path(REPO_ROOT / CONTRACT_PATH)


@pytest.mark.ac("AC-1.3.3")
@schema.parametrize()
@settings(max_examples=50, deadline=None)
def test_api__every_operation__conforms_to_the_contract(
    case: schemathesis.Case[Any],
) -> None:
    # A client per example, closed afterwards. Schemathesis's own ASGI client keeps the app's
    # lifespan running until the interpreter exits, which leaks streams between tests.
    request = case.as_transport_kwargs(base_url=BASE_URL)
    cookies = request.pop("cookies", None) or {}  # httpx takes cookies on the client only
    with TestClient(create_app(), base_url=BASE_URL, cookies=cookies) as client:
        response = client.request(**request)

    case.validate_response(response)
