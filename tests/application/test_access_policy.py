"""Policy engine v0: the permission matrix as code, deny by default (FR-IAM-02, AC-2.4.1).

The expected grants are parsed from `docs/architecture/PERMISSION_MATRIX.md` itself, so the
documented matrix and the enforced one cannot drift apart.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from htco_application.access.policy import (
    PERMISSIONS,
    STEP_UP_WINDOW,
    Capability,
    Decision,
    Grant,
    Outcome,
    Principal,
    Scope,
    decide,
)
from htco_domain.identity import AssuranceLevel, Role

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "PERMISSION_MATRIX.md"

# Matrix row label -> capability.
ROWS = {
    "View overview and KPIs": Capability.VIEW_OVERVIEW,
    "Manage users, roles, shop access": Capability.MANAGE_ACCESS,
    "Connect or disconnect a shop": Capability.CONNECT_SHOP,
    "View orders and fulfillment": Capability.VIEW_ORDERS,
    "View buyer contact details": Capability.VIEW_BUYER_CONTACT,
    "View incidents": Capability.VIEW_INCIDENTS,
    "Open or update incidents": Capability.UPDATE_INCIDENTS,
    "Retry allowlisted sync": Capability.RETRY_SYNC,
    "Confirm a shipment write to Etsy": Capability.CONFIRM_SHIPMENT_WRITE,
    "Request a bulk re-sync": Capability.REQUEST_BULK_RESYNC,
    "View tasks and approvals": Capability.VIEW_TASKS,
    "Decide low-risk approvals": Capability.DECIDE_LOW_RISK_APPROVAL,
    "Decide high-risk approvals (money, publishing, permissions)": (
        Capability.DECIDE_HIGH_RISK_APPROVAL
    ),
    "View finance and export CSV": Capability.VIEW_FINANCE,
    "Edit model policies, budgets, kill switches": Capability.EDIT_MODEL_POLICIES,
    "Enter or edit product facts": Capability.EDIT_PRODUCT_FACTS,
    "Run a Listing Health audit": Capability.RUN_LISTING_HEALTH_AUDIT,
    "View agent directory and costs": Capability.VIEW_AGENT_DIRECTORY,
    "View agent runs and traces": Capability.VIEW_AGENT_RUNS,
    "View audit log": Capability.VIEW_AUDIT_LOG,
}
ROLE_COLUMNS = [
    Role.OWNER,
    Role.ADMIN,
    Role.OPERATIONS,
    Role.FINANCE,
    Role.SUPPORT,
    Role.DEVELOPER,
    Role.VIEWER,
]


def _cell_grant(cell: str) -> Grant | None:
    """Parse one matrix cell such as `✓ MFA RA`, `S A` or `S (masked)`; `—` is no grant."""
    if cell == "—":
        return None
    tokens = re.sub(r"\(masked\)", "MASKED", cell).split()
    scope = {"✓": Scope.ORGANIZATION, "S": Scope.GRANTED_SHOPS}[tokens[0]]
    return Grant(
        scope=scope,
        mfa="MFA" in tokens,
        step_up="RA" in tokens,
        via_approval="A" in tokens,
        masked="MASKED" in tokens,
    )


def _documented_matrix() -> dict[Capability, dict[Role, Grant]]:
    text = MATRIX.read_text(encoding="utf-8")
    section = text.split("## 1. Human roles", 1)[1].split("\n## ", 1)[0]
    matrix: dict[Capability, dict[Role, Grant]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 8 or cells[0] in {"Capability", "---"} or set(cells[0]) <= {"-"}:
            continue
        grants = {
            role: _cell_grant(cell) for role, cell in zip(ROLE_COLUMNS, cells[1:], strict=True)
        }
        matrix[ROWS[cells[0]]] = {role: grant for role, grant in grants.items() if grant}
    return matrix


DOCUMENTED = _documented_matrix()
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SHOP_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
SHOP_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")


def a_principal(
    role: Role,
    *,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    reauthenticated_at: datetime | None = NOW,
    shops: frozenset[uuid.UUID] = frozenset({SHOP_A}),
) -> Principal:
    return Principal(
        role=role,
        assurance=assurance,
        reauthenticated_at=reauthenticated_at,
        granted_shops=shops,
    )


# --- the matrix --------------------------------------------------------------------------


def test_documented_matrix__covers_every_capability() -> None:
    assert set(DOCUMENTED) == set(Capability)


@pytest.mark.ac("AC-2.4.1")
@pytest.mark.parametrize("capability", list(Capability))
def test_permissions__match_the_documented_matrix(capability: Capability) -> None:
    assert PERMISSIONS[capability] == DOCUMENTED[capability]


@pytest.mark.ac("AC-2.4.1")
@pytest.mark.parametrize("role", ROLE_COLUMNS)
@pytest.mark.parametrize("capability", list(Capability))
def test_decide__fully_assured_principal__gets_exactly_the_documented_answer(
    role: Role, capability: Capability
) -> None:
    decision = decide(a_principal(role), capability, shop_id=SHOP_A, now=NOW)

    grant = DOCUMENTED[capability].get(role)
    if grant is None:
        assert decision == Decision(Outcome.DENY)
    else:
        assert decision.outcome is Outcome.ALLOW
        assert decision.via_approval is grant.via_approval
        assert decision.masked is grant.masked
        assert decision.scope is grant.scope


# --- shops -------------------------------------------------------------------------------


def test_decide__shop_scoped_role__is_denied_a_shop_it_was_not_granted() -> None:
    decision = decide(a_principal(Role.SUPPORT), Capability.VIEW_ORDERS, shop_id=SHOP_B, now=NOW)

    assert decision == Decision(Outcome.DENY)


def test_decide__organization_role__reaches_every_shop() -> None:
    decision = decide(
        a_principal(Role.ADMIN, shops=frozenset()), Capability.VIEW_ORDERS, shop_id=SHOP_B, now=NOW
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.scope is Scope.ORGANIZATION


def test_decide__listing_without_a_shop__is_limited_to_granted_shops() -> None:
    decision = decide(a_principal(Role.FINANCE), Capability.VIEW_INCIDENTS, shop_id=None, now=NOW)

    assert decision.outcome is Outcome.ALLOW
    assert decision.scope is Scope.GRANTED_SHOPS


def test_decide__viewer_orders__are_masked() -> None:
    decision = decide(a_principal(Role.VIEWER), Capability.VIEW_ORDERS, shop_id=SHOP_A, now=NOW)

    assert decision.masked is True


def test_decide__operations_shipment_write__becomes_an_approval() -> None:
    decision = decide(
        a_principal(Role.OPERATIONS), Capability.CONFIRM_SHIPMENT_WRITE, shop_id=SHOP_A, now=NOW
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.via_approval is True


# --- assurance and step-up -----------------------------------------------------------------


def test_decide__mfa_capability_at_aal1__requires_mfa() -> None:
    principal = a_principal(Role.OWNER, assurance=AssuranceLevel.AAL1)

    decision = decide(principal, Capability.MANAGE_ACCESS, shop_id=None, now=NOW)

    assert decision == Decision(Outcome.MFA_REQUIRED)


def test_decide__capability_without_mfa_at_aal1__is_allowed() -> None:
    principal = a_principal(Role.OWNER, assurance=AssuranceLevel.AAL1)

    assert decide(principal, Capability.VIEW_ORDERS, shop_id=None, now=NOW).outcome is (
        Outcome.ALLOW
    )


@pytest.mark.parametrize(
    ("reauthenticated", "outcome"),
    [
        (NOW, Outcome.ALLOW),
        (NOW - STEP_UP_WINDOW + timedelta(seconds=1), Outcome.ALLOW),
        (NOW - STEP_UP_WINDOW, Outcome.STEP_UP_REQUIRED),
        (NOW - timedelta(hours=3), Outcome.STEP_UP_REQUIRED),
        (None, Outcome.STEP_UP_REQUIRED),
        (NOW + timedelta(minutes=1), Outcome.STEP_UP_REQUIRED),
    ],
)
def test_decide__step_up_capability__needs_a_reauthentication_within_ten_minutes(
    reauthenticated: datetime | None, outcome: Outcome
) -> None:
    principal = a_principal(Role.OWNER, reauthenticated_at=reauthenticated)

    decision = decide(principal, Capability.CONNECT_SHOP, shop_id=None, now=NOW)

    assert decision.outcome is outcome


def test_step_up_window__is_ten_minutes() -> None:
    assert timedelta(minutes=10) == STEP_UP_WINDOW


def test_decide__mfa_is_checked_before_step_up() -> None:
    principal = a_principal(Role.OWNER, assurance=AssuranceLevel.AAL1, reauthenticated_at=None)

    decision = decide(principal, Capability.EDIT_MODEL_POLICIES, shop_id=None, now=NOW)

    assert decision.outcome is Outcome.MFA_REQUIRED


def test_decide__naive_clock__is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        decide(
            a_principal(Role.OWNER),
            Capability.CONNECT_SHOP,
            shop_id=None,
            now=datetime(2026, 10, 14, 12, 0),  # noqa: DTZ001 - the point of the test
        )
