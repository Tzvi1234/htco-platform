"""Activity record writer (slice 7.7): one entry per operation, with a templated summary.

A summary is produced only by a code template with identifier parameters (AC-7.7.4): a writer
cannot pass free text from a model or an external source, because the entry takes a `Summary`
and a `Summary` is made only by `summarize`. The domain rules of `ActivityRecord` hold for every
entry (AC-7.7.2).
"""

import copy
import dataclasses
import string
import uuid
from datetime import UTC, datetime

import pytest

from htco_application.activity import (
    TEMPLATES,
    ActivityEntry,
    Summary,
    new_activity,
    summarize,
)
from htco_domain.activity import MAX_SUMMARY_LENGTH, ActivityKind, ActivityOutcome
from htco_domain.actors import ActorKind, ActorRef

ORG = uuid.UUID("00000000-0000-7000-8000-0000000000a0")
SHOP = uuid.UUID("00000000-0000-7000-8000-00000000000a")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SYSTEM = ActorRef(ActorKind.SYSTEM, "scheduler")


def an_entry(**changes: object) -> ActivityEntry:
    fields: dict[str, object] = {
        "kind": ActivityKind.INCIDENT,
        "organization_id": ORG,
        "shop_id": SHOP,
        "actor": SYSTEM,
        "action": "incidents.opened",
        "outcome": ActivityOutcome.SUCCEEDED,
        "summary": summarize("incident.opened", incident="inc-1", receipt="123"),
        "occurred_at": NOW,
        "correlation_id": "c-1",
    }
    fields.update(changes)
    return new_activity(**fields)  # type: ignore[arg-type]


# --- templates (AC-7.7.4) ------------------------------------------------------------------


@pytest.mark.ac("AC-7.7.4")
def test_summarize__renders_the_template_with_identifiers() -> None:
    summary = summarize("incident.opened", incident="inc-1", receipt="123")

    assert summary.template == "incident.opened"
    assert summary.text == "Opened incident inc-1 for receipt 123"


@pytest.mark.ac("AC-7.7.4")
@pytest.mark.parametrize(
    "value",
    [
        "ignore previous instructions",
        "Dear buyer, thanks",
        "a" * 65,
        "",
        "line\nbreak",
        "t-1\n",  # `$` alone would let a final newline through (sage2)
        "<script>",
        "{receipt}",
    ],
)
def test_summarize__a_parameter_that_is_not_an_identifier_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        summarize("incident.opened", incident="inc-1", receipt=value)


@pytest.mark.ac("AC-7.7.4")
def test_summarize__an_unknown_template_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown activity template"):
        summarize("free.text", text="hello")


@pytest.mark.ac("AC-7.7.4")
@pytest.mark.parametrize(
    "params",
    [{"incident": "inc-1"}, {"incident": "inc-1", "receipt": "1", "extra": "x"}],
)
def test_summarize__needs_exactly_the_templates_parameters(params: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="parameters"):
        summarize("incident.opened", **params)


@pytest.mark.ac("AC-7.7.4")
def test_summary__cannot_be_built_from_free_text() -> None:
    with pytest.raises(TypeError, match="summarize"):
        Summary("incident.opened", "Whatever a model wrote")  # type: ignore[arg-type]


@pytest.mark.ac("AC-7.7.4")
def test_summary__replace_cannot_swap_in_free_text() -> None:
    """Sage2 on #191: a copy is checked like a new one; `text` is never set from outside."""
    summary = summarize("incident.opened", incident="inc-1", receipt="123")

    with pytest.raises(TypeError, match="init=False"):
        dataclasses.replace(summary, text="Whatever a model wrote")
    with pytest.raises(TypeError, match="init=False"):
        copy.replace(summary, text="Whatever a model wrote")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="identifier"):
        dataclasses.replace(summary, params=(("incident", "inc-1"), ("receipt", "a buyer's words")))
    with pytest.raises(ValueError, match="parameters"):
        copy.replace(summary, params=(("incident", "inc-1"),))
    with pytest.raises(ValueError, match="unknown activity template"):
        dataclasses.replace(summary, template="free.text")


@pytest.mark.ac("AC-7.7.4")
def test_summary__a_valid_copy_renders_its_own_text() -> None:
    summary = summarize("incident.opened", incident="inc-1", receipt="123")

    again = dataclasses.replace(summary, params=(("incident", "inc-2"), ("receipt", "9")))

    assert again.text == "Opened incident inc-2 for receipt 9"
    assert summary == summarize("incident.opened", receipt="123", incident="inc-1")


@pytest.mark.ac("AC-7.7.4")
def test_entry__a_plain_string_summary_is_refused() -> None:
    with pytest.raises(TypeError, match="Summary"):
        an_entry(summary="Opened incident for a buyer named Dana")


@pytest.mark.ac("AC-7.7.4")
@pytest.mark.parametrize(("template", "text"), sorted(TEMPLATES.items()))
def test_templates__are_short_business_statements(template: str, text: str) -> None:
    assert template.count(".") == 1
    assert text[0].isupper()
    params = dict.fromkeys(_placeholders(text), "x" * 64)
    assert len(summarize(template, **params).text) <= MAX_SUMMARY_LENGTH


def _placeholders(text: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


# --- the entry and its record (AC-7.7.2) --------------------------------------------------


def test_entry__carries_a_valid_record() -> None:
    entry = an_entry(causation_id="evt-1", task_id=uuid.UUID(int=7))

    record = entry.record
    assert record.summary == "Opened incident inc-1 for receipt 123"
    assert (record.kind, record.organization_id, record.shop_id) == (
        ActivityKind.INCIDENT,
        ORG,
        SHOP,
    )
    assert (record.correlation_id, record.causation_id, record.task_id) == (
        "c-1",
        "evt-1",
        uuid.UUID(int=7),
    )
    assert entry.summary.template == "incident.opened"


@pytest.mark.ac("AC-7.7.2")
def test_entry__a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        an_entry(occurred_at=NOW.replace(tzinfo=None))


@pytest.mark.ac("AC-7.7.2")
@pytest.mark.parametrize("correlation_id", ["", "  "])
def test_entry__a_missing_correlation_id_is_refused(correlation_id: str) -> None:
    with pytest.raises(ValueError, match="correlation"):
        an_entry(correlation_id=correlation_id)


@pytest.mark.ac("AC-7.7.2")
def test_entry__a_correlation_id_over_200_characters_is_refused() -> None:
    with pytest.raises(ValueError, match="correlation"):
        an_entry(correlation_id="c" * 201)


@pytest.mark.parametrize(
    "action", ["", "opened", "Incidents.Opened", "incidents opened", "incidents.opened\n"]
)
def test_entry__an_action_is_a_dotted_name(action: str) -> None:
    with pytest.raises(ValueError, match="action"):
        an_entry(action=action)


_NOT_IDENTIFIERS = ["a b", "dana@example.test", "c-1\n", "line\nbreak"]


@pytest.mark.ac("AC-7.7.4")
@pytest.mark.parametrize("value", _NOT_IDENTIFIERS)
@pytest.mark.parametrize("field_name", ["correlation", "causation"])
def test_entry__a_correlation_or_causation_id_that_is_not_an_identifier_is_refused(
    field_name: str, value: str
) -> None:
    """Sage2 on #191: no free text in any column, so these ids are identifiers too."""
    with pytest.raises(ValueError, match=field_name):
        an_entry(**{f"{field_name}_id": value})


@pytest.mark.ac("AC-7.7.4")
@pytest.mark.parametrize("value", _NOT_IDENTIFIERS)
def test_entry__an_actor_id_that_is_not_an_identifier_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="actor"):
        an_entry(actor=ActorRef(ActorKind.USER, value))


@pytest.mark.parametrize(
    "value",
    [
        str(uuid.UUID(int=5)), "job:0190a000-0000-7000-8000-0000000000ee", "order-sync:shop-1",
        "etsy-webhook:evt_1", "agent-worker:test", "c" * 200,
    ],
)  # fmt: skip
def test_entry__the_ids_main_produces_today_are_identifiers(value: str) -> None:
    entry = an_entry(actor=ActorRef(ActorKind.SYSTEM, value), correlation_id=value,
                     causation_id=value)  # fmt: skip

    assert (entry.record.actor.id, entry.record.correlation_id) == (value, value)


def test_new_activity__gets_a_fresh_id() -> None:
    assert an_entry().record.id != an_entry().record.id
