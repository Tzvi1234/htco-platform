"""Render the API's OpenAPI document and keep `packages/contracts/openapi.yaml` in step.

python -m tools.openapi --check   # fail when the committed contract differs from the API
python -m tools.openapi --write   # regenerate it (then review the diff before committing)
"""

import argparse
import difflib
import sys
from pathlib import Path
from typing import override

import yaml
from fastapi import FastAPI

from htco_api.app import create_app

CONTRACT_PATH = Path("packages") / "contracts" / "openapi.yaml"
_MAX_DIFF_LINES = 60


class _NoAliasDumper(yaml.SafeDumper):
    """Write repeated objects in full; YAML anchors and aliases confuse client generators."""

    @override
    def ignore_aliases(self, data: object) -> bool:
        return True


def render(app: FastAPI) -> str:
    """The app's OpenAPI document as YAML, keys in FastAPI's order."""
    return yaml.dump(
        app.openapi(), Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=100
    )


def contract_drift(app: FastAPI, contract: Path) -> str | None:
    """Explain how the committed contract differs from the app, or return None if it matches."""
    if not contract.exists():
        return f"{contract} is missing. Generate it with `uv run just openapi`."
    committed = contract.read_text(encoding="utf-8")
    expected = render(app)
    if committed == expected:
        return None
    diff = difflib.unified_diff(
        committed.splitlines(), expected.splitlines(), "committed", "from the API", lineterm=""
    )
    shown = "\n".join(list(diff)[:_MAX_DIFF_LINES])
    return (
        f"{contract} differs from the API. Regenerate it with `uv run just openapi`, "
        f"review the diff and commit it.\n{shown}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Keep the OpenAPI contract in step with the API.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when the contract is stale")
    mode.add_argument("--write", action="store_true", help="regenerate the contract")
    args = parser.parse_args(argv)

    app = create_app()
    if args.write:
        CONTRACT_PATH.write_text(render(app), encoding="utf-8", newline="\n")
        return 0
    drift = contract_drift(app, CONTRACT_PATH)
    if drift is None:
        return 0
    sys.stderr.write(f"{drift}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
