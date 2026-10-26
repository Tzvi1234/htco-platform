"""A completed execution keeps its handler output only when the automation's output contract
(packages/contracts/automations/<id>.output.json) accepts it (#127): identifiers and counts,
never buyer data. Invented values only."""

import logging
from pathlib import Path

import pytest

from htco_application.automation.outputs import ContractOutputs

CONTRACTS = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "automations"


def test_output__its_contract_accepts_it__kept() -> None:
    outputs = ContractOutputs(CONTRACTS)

    kept = outputs.check("tracking-sync", {"receipts": 2, "recorded": 1, "via": "schedule"})

    assert kept == {"receipts": 2, "recorded": 1, "via": "schedule"}


def test_output__a_field_its_contract_does_not_allow__not_kept_and_logged_without_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outputs = ContractOutputs(CONTRACTS)

    with caplog.at_level(logging.WARNING, logger="htco_application.automation.outputs"):
        kept = outputs.check(
            "tracking-sync", {"receipts": 2, "recorded": 1, "buyer_name": "Fake Buyer"}
        )

    assert kept is None
    [record] = [r for r in caplog.records if r.getMessage() == "automation.output_rejected"]
    assert getattr(record, "automation_id", None) == "tracking-sync"
    assert "Fake Buyer" not in caplog.text


def test_output__an_automation_without_an_output_contract__not_kept() -> None:
    outputs = ContractOutputs(CONTRACTS)

    assert outputs.check("an-automation-without-a-contract", {"count": 1}) is None


def test_output__an_unsafe_automation_id__never_reads_outside_the_folder(tmp_path: Path) -> None:
    (tmp_path / "secret.output.json").write_text('{"type": "object"}', encoding="utf-8")
    outputs = ContractOutputs(tmp_path / "contracts")

    assert outputs.check("../secret", {"count": 1}) is None
