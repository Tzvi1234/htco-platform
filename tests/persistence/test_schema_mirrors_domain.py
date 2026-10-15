"""The database's closed lists match the domain enums, so a new role or actor kind cannot drift."""

import re
from pathlib import Path

from htco_domain.actors import ActorKind
from htco_domain.identity import Role
from htco_domain.integrations import IntegrationProvider, IntegrationStatus

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def _check_values(constraint: str) -> set[str]:
    sql = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql")))
    # Migrations run in file order, so the last definition of a constraint is the effective one.
    matches = re.findall(rf"constraint {constraint} check \(\s*\w+ in \(([^)]*)\)", sql)
    assert matches, f"check constraint {constraint} not found in the migrations"
    return set(re.findall(r"'([^']+)'", matches[-1]))


def test_memberships_role_check__lists_exactly_the_domain_roles() -> None:
    assert _check_values("memberships_role_check") == {role.value for role in Role}


def test_audit_actor_kind_check__lists_exactly_the_domain_actor_kinds() -> None:
    assert _check_values("audit_log_actor_kind_check") == {kind.value for kind in ActorKind}


def test_shop_integrations_status_check__lists_exactly_the_domain_statuses() -> None:
    assert _check_values("shop_integrations_status_check") == {s.value for s in IntegrationStatus}


def test_shop_integrations_provider_check__lists_exactly_the_domain_providers() -> None:
    assert _check_values("shop_integrations_provider_check") == {
        provider.value for provider in IntegrationProvider
    }
