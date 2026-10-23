"""Automation definitions as database rows, the registry sync's plan, and execution summaries
(slice 5.10, AC-5.10.7).

The database holds every version of every reviewed document in `registry/automations/`. The sync
command compares the files with the current rows by the SHA-256 of each document: a new ID or
version is `added`, a changed document of the same version is `changed`, and an automation whose
file disappeared is `missing`, which the sync refuses (rows are never deleted: retire an automation
with `readiness = "retired"` in its file). Applying the plan twice changes nothing.
"""

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from htco_application.workforce.registry import Registry, parse_automation
from htco_domain.actors import ActorRef
from htco_domain.automation import AutomationDefinition, ExecutionState, TriggerKind


def document_hash(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DefinitionRow:
    document: Mapping[str, Any]
    specification_hash: str
    registry_commit: str
    synced_at: datetime

    @property
    def key(self) -> tuple[str, int]:
        return str(self.document["id"]), int(self.document["version"])


class SyncKind(StrEnum):
    ADDED = "added"
    CHANGED = "changed"
    MISSING = "missing"  # in the database, no longer in the registry: refused


@dataclass(frozen=True)
class SyncChange:
    kind: SyncKind
    automation_id: str
    version: int
    hash_before: str | None
    hash_after: str | None


@dataclass(frozen=True)
class SyncPlan:
    changes: tuple[SyncChange, ...]
    unchanged: int
    rows: tuple[tuple[AutomationDefinition, DefinitionRow], ...]  # what applying writes

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def blocked(self) -> bool:
        return any(change.kind is SyncKind.MISSING for change in self.changes)


def plan_sync(
    documents: Iterable[Mapping[str, Any]],
    current: Sequence[DefinitionRow],
    *,
    commit: str,
    now: datetime,
) -> SyncPlan:
    """Compare the reviewed documents (already validated by the registry) with the stored rows."""
    stored = {row.key: row for row in current}
    latest_ids = {row.key[0] for row in current}
    changes: list[SyncChange] = []
    rows: list[tuple[AutomationDefinition, DefinitionRow]] = []
    unchanged = 0
    seen_ids: set[str] = set()
    for document in documents:
        definition = parse_automation(document)
        seen_ids.add(definition.id)
        digest = document_hash(document)
        before = stored.get((definition.id, definition.version))
        if before is not None and before.specification_hash == digest:
            unchanged += 1
            continue
        kind = SyncKind.ADDED if before is None else SyncKind.CHANGED
        changes.append(
            SyncChange(
                kind,
                definition.id,
                definition.version,
                None if before is None else before.specification_hash,
                digest,
            )
        )
        rows.append(
            (
                definition,
                DefinitionRow(dict(document), digest, registry_commit=commit, synced_at=now),
            )
        )
    for missing in sorted(latest_ids - seen_ids):
        row = max((r for r in current if r.key[0] == missing), key=lambda r: r.key[1])
        changes.append(
            SyncChange(SyncKind.MISSING, missing, row.key[1], row.specification_hash, None)
        )
    return SyncPlan(changes=tuple(changes), unchanged=unchanged, rows=tuple(rows))


def registry_from_rows(rows: Iterable[DefinitionRow]) -> Registry:
    """The engine's registry: the stored current definitions. Employees join from Phase 8, so
    until then an employee's trigger is refused as unknown."""
    automations = {}
    for row in rows:
        definition = parse_automation(row.document)
        automations[definition.id] = definition
    return Registry(
        employees=MappingProxyType({}),
        tools=MappingProxyType({}),
        automations=MappingProxyType(automations),
        advisors=MappingProxyType({}),
    )


@dataclass(frozen=True)
class ExecutionSummary:
    id: uuid.UUID
    automation_id: str
    automation_version: int
    shop_id: uuid.UUID | None
    trigger: TriggerKind
    actor: ActorRef
    state: ExecutionState
    attempts: int
    correlation_id: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ExecutionPage:
    items: tuple[ExecutionSummary, ...]
    next_before: uuid.UUID | None
