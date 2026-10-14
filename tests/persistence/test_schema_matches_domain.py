"""The database's allowed values match the domain enums, so a role or an actor kind added in
Python cannot silently be refused by Postgres, or the other way round (slices 2.2 and 2.5)."""

import re
from pathlib import Path

import pytest

from htco_domain.actors import ActorKind
from htco_domain.identity import Role

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def _domain_values(name: str) -> set[str]:
    sql = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql")))
    match = re.search(
        rf"create domain {re.escape(name)} as text\s+check \(value in \(([^)]*)\)\)", sql
    )
    assert match is not None, f"domain {name} not found in the migrations"
    return set(re.findall(r"'([^']*)'", match.group(1)))


@pytest.mark.ac("AC-2.2.1")
def test_member_role_domain__matches_the_role_enum() -> None:
    assert _domain_values("app.member_role") == {role.value for role in Role}


@pytest.mark.ac("AC-2.5.3")
def test_actor_kind_domain__matches_the_actor_kind_enum() -> None:
    assert _domain_values("audit.actor_kind") == {kind.value for kind in ActorKind}
