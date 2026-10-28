"""The registry's TOML files (`registry/`): each employee, automation and advisor is one file;
tools are grouped by family (`[[tools]]` tables)."""

import json
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from htco_application.execution.contracts import ActionCatalog, ActionCatalogError
from htco_application.workforce.registry import RESTRICTED_GRANTS, Registry
from htco_domain.actors import ActorKind


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


def _actor_kinds(document: Mapping[str, Any]) -> frozenset[ActorKind] | None:
    """An action's `actor_kinds`: who may submit it (sage2 on #207). Absent: not restricted by
    kind. Raises ValueError for anything but a non-empty list of distinct actor kinds."""
    if "actor_kinds" not in document:
        return None
    raw = document["actor_kinds"]
    if not isinstance(raw, list) or not raw or not all(isinstance(k, str) for k in raw):
        raise ValueError("actor_kinds is a non-empty list of actor kinds")
    if len(set(raw)) != len(raw):
        raise ValueError("actor_kinds names each kind once")
    try:
        return frozenset(ActorKind(k) for k in raw)
    except ValueError:
        raise ValueError(f"actor_kinds names an unknown kind: {sorted(raw)}") from None


def _actor_kind_problems(documents: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    for document in documents:
        name = str(document.get("tool", "?"))
        try:
            kinds = _actor_kinds(document)
        except ValueError as error:
            problems.append(f"action {name!r}: {error}")
            continue
        if kinds is not None and ActorKind.USER not in kinds and "user_capability" in document:
            problems.append(f"action {name!r}: a user_capability needs actor_kinds to name user")
        if name in RESTRICTED_GRANTS and kinds != frozenset({ActorKind.AUTOMATION}):
            problems.append(f"action {name!r}: a restricted read names actor_kinds = [automation]")
    return problems


def read_action_actor_kinds(root: Path) -> Mapping[str, frozenset[ActorKind]]:
    """Each action's `actor_kinds`, by action name, for the Action Gateway to refuse any other
    kind before routing (orion's part B of #147). Actions without the key are not listed.
    Raises `ActionCatalogError` like `read_action_catalog`."""
    documents = [a for family in _documents(root / "actions") for a in family.get("actions", [])]
    problems = _actor_kind_problems(documents)
    if problems:
        raise ActionCatalogError(problems)
    found = {str(d["tool"]): _actor_kinds(d) for d in documents}
    return MappingProxyType({name: kinds for name, kinds in found.items() if kinds is not None})


def read_action_catalog(root: Path, schemas: Path, registry: Registry) -> ActionCatalog:
    """The action contracts of `registry/actions/*.toml` with their JSON Schemas (slice 5.12),
    their `actor_kinds` validated too; raises `ActionCatalogError` listing every problem."""
    families = list(_documents(root / "actions"))
    documents = [action for family in families for action in family.get("actions", [])]
    problems = _actor_kind_problems(documents)
    if problems:
        raise ActionCatalogError(problems)
    limits = [limit for family in families for limit in family.get("rate_limits", [])]
    loaded = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(schemas.glob("*.json"))
    }
    return ActionCatalog.from_documents(
        documents, tools=registry.tools, schemas=loaded, rate_limits=limits
    )
