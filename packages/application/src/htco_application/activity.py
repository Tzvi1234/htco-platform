"""Activity record writer (slice 7.7, ADR-0012 rule 4, ER-AI-16).

Every use case that changes state writes one `ActivityEntry` through an `ActivityWriter`, in its
own transaction, next to its audit record or events. An entry's summary is a `Summary`: a code
template from `TEMPLATES` and its identifier parameters (ids, receipt numbers, counts). A
`Summary` is valid by construction: it renders its own `text`, which is never an argument, so
every route to one (`summarize`, the constructor, `dataclasses.replace`, `copy.replace`) is
checked (sage2 on #191). The actor, correlation and causation ids are identifiers too. So no
prompt, model output, buyer text or raw external payload can reach `activity_records`
(AC-7.7.4). The template key is stored with the text, so a screen can show the statement in the
reader's language later.
"""

import re
import string
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from htco_domain.activity import ActivityKind, ActivityOutcome, ActivityRecord
from htco_domain.actors import ActorRef

MAX_CORRELATION_ID = 200
# Always `fullmatch`: `$` alone lets a final newline through.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:#/-]{0,63}")  # a summary parameter
# The actor, correlation and causation ids; the migration's checks use the same pattern.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:#/-]{0,199}")
_ACTION = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")

# Business statements, one per kind of operation. Parameters are identifiers only.
TEMPLATES: Mapping[str, str] = MappingProxyType(
    {
        "approval.decided": "Approval {approval} {decision} by {decider}",
        "approval.requested": "Requested approval {approval} for {action}",
        "automation.finished": "Automation {automation} run {execution} ended {state}",
        "incident.moved": "Incident {incident} moved from {from_state} to {to_state}",
        "incident.opened": "Opened incident {incident} for receipt {receipt}",
        "notification.sent": "Sent notification {notification} of kind {kind}",
        "policy.decided": "Policy {version} answered {verdict} for {capability}",
        "task.moved": "Task {task} moved from {from_state} to {to_state}",
    }
)


def _placeholders(text: str) -> frozenset[str]:
    return frozenset(name for _, name, _, _ in string.Formatter().parse(text) if name)


@dataclass(frozen=True)
class Summary:
    """A code template and its identifier parameters, rendered here. Build it with `summarize`.

    `text` is not an init field: `replace(summary, text=...)` raises, and any other copy runs
    these checks again."""

    template: str
    params: tuple[tuple[str, str], ...]
    text: str = field(init=False)

    def __post_init__(self) -> None:
        try:  # text where the parameters belong is not a set of (name, value) pairs
            params = dict(self.params)
        except TypeError, ValueError:
            raise TypeError("build a Summary with summarize(template, **identifiers)") from None
        text = TEMPLATES.get(self.template)
        if text is None:
            raise ValueError(f"unknown activity template {self.template!r}")
        expected = _placeholders(text)
        if len(params) != len(self.params) or set(params) != expected:
            raise ValueError(f"{self.template} takes the parameters {sorted(expected)}")
        for name, value in params.items():
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{self.template}: {name} must be an identifier")
        object.__setattr__(self, "text", text.format(**params))


def summarize(template: str, **params: str) -> Summary:
    return Summary(template, tuple(sorted(params.items())))


@dataclass(frozen=True)
class ActivityEntry:
    """One record to write, with the template that produced its summary. Every entry is
    checked here, however it was made (`new_activity`, the constructor or `replace`): the
    writer stores only what these checks allow (quill2 on #191)."""

    record: ActivityRecord
    summary: Summary

    def __post_init__(self) -> None:
        if not isinstance(self.summary, Summary):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("an activity summary must be a Summary from summarize()")
        if self.record.summary != self.summary.text:
            raise ValueError("the record's summary must be the rendered template")
        record = self.record
        if not _ACTION.fullmatch(record.action):
            raise ValueError(f"an activity action is a dotted name, not {record.action[:40]!r}")
        # Identifiers only (sage2 on #191): never a buyer's message, an address or model output.
        for name, value in (
            ("actor", record.actor.id),
            ("correlation", record.correlation_id),
            ("causation", record.causation_id),
        ):
            if value is not None and not _ID.fullmatch(value):
                raise ValueError(
                    f"a {name} id is an identifier of 1 to {MAX_CORRELATION_ID} characters"
                )


def new_activity(  # noqa: PLR0913 - one argument per field of the record
    *,
    kind: ActivityKind,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID | None,
    actor: ActorRef,
    action: str,
    outcome: ActivityOutcome,
    summary: Summary,
    occurred_at: datetime,
    correlation_id: str,
    causation_id: str | None = None,
    task_id: uuid.UUID | None = None,
    ids: Callable[[], uuid.UUID] = uuid.uuid7,
) -> ActivityEntry:
    if not isinstance(summary, Summary):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("an activity summary must be a Summary from summarize()")
    record = ActivityRecord(
        id=ids(),
        kind=kind,
        organization_id=organization_id,
        shop_id=shop_id,
        actor=actor,
        action=action,
        outcome=outcome,
        summary=summary.text,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        causation_id=causation_id,
        task_id=task_id,
    )
    return ActivityEntry(record, summary)


class ActivityWriter(Protocol):
    """Appends entries inside the caller's transaction (like `AuditWriter`)."""

    def append_activity(self, entry: ActivityEntry) -> None: ...
