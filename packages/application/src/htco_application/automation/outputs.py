"""Which handler outputs a completed execution keeps (#127).

A handler returns `Completed(output)`: per-run identifiers and counts, for example Tracking Sync's
`{"receipts": 2, "recorded": 1}`. The engine keeps it with the execution only when the
automation's output contract (`packages/contracts/automations/<id>.output.json`) accepts it, so
what is stored is schema-checked and never buyer data (the contracts allow no buyer field). An
automation without an output contract keeps nothing, and a refused output is logged by the
automation's ID and the number of problems only, never its values.
"""

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from htco_application.execution.schema import check_schema, validate
from htco_application.jobs import JsonValue

_log = logging.getLogger(__name__)
_AUTOMATION_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class ContractOutputs:
    """`OutputCheck` from the output contracts in one folder, each read once."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._schemas: dict[str, dict[str, Any] | None] = {}

    def _schema(self, automation_id: str) -> dict[str, Any] | None:
        if automation_id not in self._schemas:
            schema: dict[str, Any] | None = None
            if _AUTOMATION_ID.match(automation_id):  # a kebab-case ID: never a path
                path = self._directory / f"{automation_id}.output.json"
                if path.is_file():
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    check_schema(loaded)
                    schema = loaded
            self._schemas[automation_id] = schema
        return self._schemas[automation_id]

    def check(
        self, automation_id: str, output: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue] | None:
        schema = self._schema(automation_id)
        if schema is None:
            return None
        problems = validate(dict(output), schema)
        if problems:
            _log.warning(
                "automation.output_rejected",
                extra={"automation_id": automation_id, "problems": len(problems)},
            )
            return None
        return dict(output)


__all__ = ["ContractOutputs"]
