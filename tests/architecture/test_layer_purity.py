"""Architecture contract: the inner layers import only the standard library and inner layers.

`htco_domain` may import only the standard library; `htco_application` may also import the
domain (ER-ARCH-01, SOFTWARE_DESIGN §4.3). import-linter covers the other contracts; this one
needs an allow-list, which import-linter cannot express.
"""

from pathlib import Path

import pytest

from tests.architecture.purity import ForbiddenImport, find_forbidden_imports

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_SOURCE = REPO_ROOT / "packages" / "domain" / "src" / "htco_domain"
APPLICATION_SOURCE = REPO_ROOT / "packages" / "application" / "src" / "htco_application"


def _describe(contract: str, violations: list[ForbiddenImport]) -> str:
    lines = [f"Broken contract: {contract}"]
    lines += [
        f"  {v.path.relative_to(REPO_ROOT)}:{v.line} imports {v.module!r}" for v in violations
    ]
    return "\n".join(lines)


@pytest.mark.ac("AC-1.1.2")
def test_domain__imports__only_the_standard_library() -> None:
    violations = find_forbidden_imports(DOMAIN_SOURCE, allowed=frozenset({"htco_domain"}))

    assert violations == [], _describe("Domain imports only the standard library", violations)


@pytest.mark.ac("AC-1.1.2")
def test_application__imports__only_the_standard_library_and_the_domain() -> None:
    allowed = frozenset({"htco_application", "htco_domain"})

    violations = find_forbidden_imports(APPLICATION_SOURCE, allowed=allowed)

    assert violations == [], _describe(
        "Application imports only the standard library and the domain", violations
    )


@pytest.mark.ac("AC-1.1.2")
def test_find_forbidden_imports__module_importing_fastapi__is_reported(tmp_path: Path) -> None:
    module = tmp_path / "orders.py"
    module.write_text("import json\n\nimport fastapi\n", encoding="utf-8")

    violations = find_forbidden_imports(tmp_path, allowed=frozenset())

    assert violations == [ForbiddenImport(path=module, line=3, module="fastapi")]


@pytest.mark.parametrize(
    "statement",
    [
        "from pydantic import BaseModel",
        "import httpx.client",
        "from htco_platform.settings import load_settings",
        "def handler():\n    import psycopg\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import sqlalchemy\n",
    ],
)
def test_find_forbidden_imports__any_import_form__is_reported(
    tmp_path: Path, statement: str
) -> None:
    (tmp_path / "module.py").write_text(statement, encoding="utf-8")

    violations = find_forbidden_imports(tmp_path, allowed=frozenset({"htco_domain"}))

    assert len(violations) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "from __future__ import annotations",
        "import dataclasses\nfrom decimal import Decimal",
        "from . import money",
        "from .orders import Order",
        "from htco_domain.money import Money",
    ],
)
def test_find_forbidden_imports__standard_library_relative_and_allowed__pass(
    tmp_path: Path, statement: str
) -> None:
    (tmp_path / "module.py").write_text(statement, encoding="utf-8")

    violations = find_forbidden_imports(tmp_path, allowed=frozenset({"htco_domain"}))

    assert violations == []


def test_find_forbidden_imports__nested_packages__are_scanned(tmp_path: Path) -> None:
    nested = tmp_path / "orders" / "events.py"
    nested.parent.mkdir()
    nested.write_text("import structlog\n", encoding="utf-8")

    violations = find_forbidden_imports(tmp_path, allowed=frozenset())

    assert [v.path for v in violations] == [nested]
