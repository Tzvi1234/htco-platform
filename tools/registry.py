"""Load and check the office registry (`registry/`): employees, tools, automations, advisors.

python -m tools.registry --check   # validate every document and print a summary

Each employee, automation and advisor is one TOML file; tools are grouped by family
(`[[tools]]` tables). The registry is reviewed data (ADR-0012): this check runs in CI.
"""

import argparse
import sys
from pathlib import Path

from htco_application.execution.contracts import ActionCatalogError
from htco_application.workforce.files import read_action_catalog, read_registry
from htco_application.workforce.registry import Registry, RegistryError

REGISTRY_ROOT = Path(__file__).resolve().parents[1] / "registry"
ACTION_SCHEMAS = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "actions"


def load_registry(root: Path = REGISTRY_ROOT) -> Registry:
    return read_registry(root).registry()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate the office registry.")
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument("--root", type=Path, default=REGISTRY_ROOT)
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.root)
        catalog = read_action_catalog(args.root, ACTION_SCHEMAS, registry)
    except (RegistryError, ActionCatalogError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    ready = sum(p.is_production_ready() for p in registry.employees.values())
    sys.stdout.write(
        f"{len(registry.employees)} employees ({ready} production-ready), "
        f"{len(registry.automations)} automations, {len(registry.tools)} tools, "
        f"{len(registry.advisors)} advisor(s), {len(catalog.actions)} action contract(s)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
