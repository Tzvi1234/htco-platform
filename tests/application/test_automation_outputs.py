"""A completed execution keeps only the part of its handler output that an explicit typed model
in code allows (#127, sage and quill on #158): a closed set of keys per automation, with counts
and IDs as integers, enumerated codes, flags and UUIDs. Anything else is dropped and logged by
the automation's ID and a count only, never a value. Invented values only."""

import logging

import pytest

from htco_application.automation.deployed import WORKER_AUTOMATIONS
from htco_application.automation.outputs import OUTPUT_MODELS, TypedOutputs


def test_every_automation_the_worker_runs__has_an_output_model() -> None:
    assert set(OUTPUT_MODELS) == WORKER_AUTOMATIONS


def test_output__every_key_allowed_and_typed__kept() -> None:
    kept = TypedOutputs().check("tracking-sync", {"receipts": 2, "recorded": 1, "via": "schedule"})

    assert kept == {"receipts": 2, "recorded": 1, "via": "schedule"}


def test_the_counters__keep_scalar_totals_never_the_per_listing_maps() -> None:
    outputs = TypedOutputs()

    single = outputs.check(
        "product-sales-counters", {"receipt_id": 7, "units": {"11": -2}, "orders": {"11": -1}}
    )
    rebuild = outputs.check("product-sales-counters", {"rebuilt": 3, "moved": 2})

    assert single == {"receipt_id": 7}
    assert rebuild == {"rebuilt": 3, "moved": 2}


@pytest.mark.parametrize(
    "output",
    [
        {"receipts": "two"},  # a string where a count belongs
        {"receipts": True},  # a flag is not a count
        {"receipts": -1},  # a count is never negative
        {"receipts": 2**63},  # out of range
        {"via": "email"},  # not one of the codes
        {"buyer_name": "Fake Buyer"},  # not a key of the model
        {"receipts": {"nested": 1}},
    ],
)
def test_output__a_key_or_value_outside_the_model__dropped_and_logged_without_values(
    output: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="htco_application.automation.outputs"):
        kept = TypedOutputs().check("tracking-sync", {"recorded": 1, **output})  # type: ignore[dict-item]

    assert kept == {"recorded": 1}
    [record] = [r for r in caplog.records if r.getMessage() == "automation.output_dropped"]
    assert getattr(record, "automation_id", None) == "tracking-sync"
    assert getattr(record, "dropped", None) == 1
    assert "Fake Buyer" not in caplog.text
    assert "email" not in caplog.text


def test_output__nothing_left_after_the_model__nothing_kept() -> None:
    assert TypedOutputs().check("tracking-sync", {"buyer_name": "Fake Buyer"}) is None


def test_output__an_automation_without_a_model__nothing_kept() -> None:
    assert TypedOutputs().check("an-automation-without-a-model", {"count": 1}) is None


def test_incident_ids__only_as_uuids() -> None:
    outputs = TypedOutputs()

    assert outputs.check(
        "incident-detection-escalation",
        {"incident_id": "0b9f2c1e-7a3d-4c55-9f0e-2d6b8a1c3e44", "opened": 1},
    ) == {"incident_id": "0b9f2c1e-7a3d-4c55-9f0e-2d6b8a1c3e44", "opened": 1}
    assert outputs.check("incident-detection-escalation", {"incident_id": "a note"}) is None
