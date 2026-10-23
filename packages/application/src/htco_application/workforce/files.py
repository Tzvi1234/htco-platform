"""The registry's TOML files (`registry/`): each employee, automation and advisor is one file;
tools are grouped by family (`[[tools]]` tables)."""

import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from htco_application.execution.contracts import ActionCatalog
from htco_application.workforce.registry import Registry


@dataclass(frozen=True)
class RegistryDocuments:
    employees: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    automations: list[dict[str, Any]]
    advisors: list[dict[str, Any]]

    def registry(self) -> Registry:
        """Validate every document and their cross-references (raises `RegistryError`)."""
        return Registry.from_documents(
            employees=self.employees,
            tools=self.tools,
            automations=self.automations,
            advisors=self.advisors,
        )


def _documents(folder: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(folder.glob("*.toml")):
        yield tomllib.loads(path.read_text(encoding="utf-8"))


def read_registry(root: Path) -> RegistryDocuments:
    tools = [tool for family in _documents(root / "tools") for tool in family.get("tools", [])]
    return RegistryDocuments(
        employees=list(_documents(root / "employees")),
        tools=tools,
        automations=list(_documents(root / "automations")),
        advisors=list(_documents(root / "advisors")),
    )


def read_action_catalog(root: Path, schemas: Path, registry: Registry) -> ActionCatalog:
    """The action contracts of `registry/actions/*.toml` with their JSON Schemas (slice 5.12);
    raises `ActionCatalogError` listing every problem."""
    documents = [
        action for family in _documents(root / "actions") for action in family.get("actions", [])
    ]
    loaded = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(schemas.glob("*.json"))
    }
    return ActionCatalog.from_documents(documents, tools=registry.tools, schemas=loaded)
