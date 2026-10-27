"""Policy engine v0: who may do what, on which shops (FR-IAM-02, PERMISSION_MATRIX §1).

Deny by default: a capability a role is not granted in `PERMISSIONS` does not exist for it,
and a shop of another organization does not exist for anyone. A grant can be
organization-wide (Owner and Admin reach every shop of their organization) or limited to the
shops granted to the user, and can require an `aal2` session (MFA), a recent re-authentication
(step-up), an approval instead of acting directly, or masked buyer data. The table mirrors
docs/architecture/PERMISSION_MATRIX.md; a test parses that document and fails on any drift.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from types import MappingProxyType

from htco_domain.identity import AssuranceLevel, Role

# A step-up (re-entering the second factor) elevates the session for this long (AC-3.5.7).
STEP_UP_WINDOW = timedelta(minutes=10)


class Capability(StrEnum):
    VIEW_OVERVIEW = "view_overview"
    MANAGE_ACCESS = "manage_access"
    CONNECT_SHOP = "connect_shop"
    VIEW_ORDERS = "view_orders"
    VIEW_BUYER_CONTACT = "view_buyer_contact"
    VIEW_INCIDENTS = "view_incidents"
    UPDATE_INCIDENTS = "update_incidents"
    RETRY_SYNC = "retry_sync"
    CONFIRM_SHIPMENT_WRITE = "confirm_shipment_write"
    REQUEST_BULK_RESYNC = "request_bulk_resync"
    VIEW_TASKS = "view_tasks"
    DECIDE_LOW_RISK_APPROVAL = "decide_low_risk_approval"
    DECIDE_HIGH_RISK_APPROVAL = "decide_high_risk_approval"
    VIEW_FINANCE = "view_finance"
    EDIT_MODEL_POLICIES = "edit_model_policies"
    EDIT_PRODUCT_FACTS = "edit_product_facts"
    RUN_LISTING_HEALTH_AUDIT = "run_listing_health_audit"
    VIEW_AGENT_DIRECTORY = "view_agent_directory"
    VIEW_AGENT_RUNS = "view_agent_runs"
    VIEW_AUDIT_LOG = "view_audit_log"
    VIEW_BROWSER_ENVIRONMENT = "view_browser_environment"
    VIEW_SUPPLIER_QUOTES = "view_supplier_quotes"  # slice 7.16: the people who choose (7.17)


class Scope(Enum):
    ORGANIZATION = "organization"
    GRANTED_SHOPS = "granted_shops"


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    MFA_REQUIRED = "mfa_required"
    STEP_UP_REQUIRED = "step_up_required"


@dataclass(frozen=True)
class Grant:
    """One cell of the matrix."""

    scope: Scope
    mfa: bool = False
    step_up: bool = False
    via_approval: bool = False
    masked: bool = False


@dataclass(frozen=True)
class Principal:
    """The acting user as the server knows them: organization, role, session strength, shops."""

    organization_id: uuid.UUID
    role: Role
    assurance: AssuranceLevel
    reauthenticated_at: datetime | None
    granted_shops: frozenset[uuid.UUID]


@dataclass(frozen=True)
class ShopRef:
    """A targeted shop and the organization that owns it, both read from the database."""

    id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    scope: Scope | None = None
    via_approval: bool = False
    masked: bool = False


_ORG = Grant(Scope.ORGANIZATION)
_ORG_MFA = Grant(Scope.ORGANIZATION, mfa=True)
_ORG_MFA_STEP_UP = Grant(Scope.ORGANIZATION, mfa=True, step_up=True)
_SHOPS = Grant(Scope.GRANTED_SHOPS)
_SHOPS_MFA = Grant(Scope.GRANTED_SHOPS, mfa=True)
_SHOPS_APPROVAL = Grant(Scope.GRANTED_SHOPS, via_approval=True)
_SHOPS_MASKED = Grant(Scope.GRANTED_SHOPS, masked=True)

_O, _AD, _OP, _FI, _SU, _DE, _VI = (
    Role.OWNER,
    Role.ADMIN,
    Role.OPERATIONS,
    Role.FINANCE,
    Role.SUPPORT,
    Role.DEVELOPER,
    Role.VIEWER,
)

_MATRIX: dict[Capability, dict[Role, Grant]] = {
    Capability.VIEW_OVERVIEW: {
        _O: _ORG,
        _AD: _ORG,
        _OP: _SHOPS,
        _FI: _SHOPS,
        _SU: _SHOPS,
        _DE: _SHOPS,
        _VI: _SHOPS,
    },
    Capability.MANAGE_ACCESS: {_O: _ORG_MFA, _AD: _ORG_MFA},
    Capability.CONNECT_SHOP: {_O: _ORG_MFA_STEP_UP},
    Capability.VIEW_ORDERS: {
        _O: _ORG,
        _AD: _ORG,
        _OP: _SHOPS,
        _FI: _SHOPS,
        _SU: _SHOPS,
        _DE: _SHOPS,
        _VI: _SHOPS_MASKED,
    },
    Capability.VIEW_BUYER_CONTACT: {_O: _ORG, _AD: _ORG, _OP: _SHOPS, _SU: _SHOPS},
    Capability.VIEW_INCIDENTS: {
        _O: _ORG,
        _AD: _ORG,
        _OP: _SHOPS,
        _FI: _SHOPS,
        _SU: _SHOPS,
        _DE: _SHOPS,
        _VI: _SHOPS,
    },
    Capability.UPDATE_INCIDENTS: {_O: _ORG, _AD: _ORG, _OP: _SHOPS, _SU: _SHOPS, _DE: _SHOPS},
    Capability.RETRY_SYNC: {_O: _ORG, _AD: _ORG, _OP: _SHOPS, _DE: _SHOPS},
    Capability.CONFIRM_SHIPMENT_WRITE: {
        _O: _ORG_MFA_STEP_UP,
        _AD: _ORG_MFA_STEP_UP,
        _OP: _SHOPS_APPROVAL,
    },
    Capability.REQUEST_BULK_RESYNC: {
        _O: _ORG,
        _AD: _ORG,
        _OP: _SHOPS_APPROVAL,
        _DE: _SHOPS_APPROVAL,
    },
    Capability.VIEW_TASKS: {
        _O: _ORG,
        _AD: _ORG,
        _OP: _SHOPS,
        _FI: _SHOPS,
        _SU: _SHOPS,
        _DE: _SHOPS,
        _VI: _SHOPS,
    },
    Capability.DECIDE_LOW_RISK_APPROVAL: {
        _O: _ORG_MFA,
        _AD: _ORG_MFA,
        _OP: _SHOPS_MFA,
        _FI: _SHOPS_MFA,
    },
    Capability.DECIDE_HIGH_RISK_APPROVAL: {_O: _ORG_MFA_STEP_UP},
    Capability.VIEW_FINANCE: {_O: _ORG_MFA, _AD: _ORG_MFA, _FI: _SHOPS_MFA},
    Capability.EDIT_MODEL_POLICIES: {_O: _ORG_MFA_STEP_UP},
    Capability.EDIT_PRODUCT_FACTS: {_O: _ORG, _AD: _ORG, _OP: _SHOPS},
    Capability.RUN_LISTING_HEALTH_AUDIT: {_O: _ORG_MFA, _AD: _ORG_MFA, _OP: _SHOPS},
    Capability.VIEW_AGENT_DIRECTORY: {_O: _ORG, _AD: _ORG, _OP: _SHOPS, _FI: _SHOPS, _DE: _SHOPS},
    Capability.VIEW_AGENT_RUNS: {_O: _ORG, _AD: _ORG, _OP: _SHOPS, _DE: _SHOPS},
    Capability.VIEW_AUDIT_LOG: {_O: _ORG, _AD: _ORG, _DE: _SHOPS},
    Capability.VIEW_BROWSER_ENVIRONMENT: {_O: _ORG, _AD: _ORG},
    Capability.VIEW_SUPPLIER_QUOTES: {_O: _ORG, _AD: _ORG},
}

# Read-only view: the matrix changes only through a reviewed change to this file and the doc.
PERMISSIONS: Mapping[Capability, Mapping[Role, Grant]] = MappingProxyType(
    {capability: MappingProxyType(grants) for capability, grants in _MATRIX.items()}
)


def _recently_reauthenticated(principal: Principal, now: datetime) -> bool:
    at = principal.reauthenticated_at
    # A time in the future is treated as no re-authentication rather than trusted.
    return at is not None and now - STEP_UP_WINDOW < at <= now


def decide(
    principal: Principal, capability: Capability, *, shop: ShopRef | None, now: datetime
) -> Decision:
    """Decide whether `principal` may use `capability`, on `shop` when one is targeted.

    A shop owned by another organization is denied whatever the role, so an organization-wide
    grant never crosses tenants. Without a `shop` (a list across shops), an allowed decision's
    `scope` says whether the caller may see every shop or only the granted ones. Assurance is
    checked before step-up, because a step-up needs an `aal2` session first.
    """
    if now.tzinfo is None:
        raise ValueError("decide needs a timezone-aware clock")
    grant = PERMISSIONS[capability].get(principal.role)
    if grant is None:
        return Decision(Outcome.DENY)
    if shop is not None:
        if shop.organization_id != principal.organization_id:
            return Decision(Outcome.DENY)
        if grant.scope is Scope.GRANTED_SHOPS and shop.id not in principal.granted_shops:
            return Decision(Outcome.DENY)
    if grant.mfa and principal.assurance is not AssuranceLevel.AAL2:
        return Decision(Outcome.MFA_REQUIRED)
    if grant.step_up and not _recently_reauthenticated(principal, now):
        return Decision(Outcome.STEP_UP_REQUIRED)
    return Decision(
        Outcome.ALLOW, scope=grant.scope, via_approval=grant.via_approval, masked=grant.masked
    )
