"""Every workspace package imports cleanly from its own source tree in this checkout."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKSPACE_PACKAGES = {
    "htco_domain": "packages/domain/src",
    "htco_application": "packages/application/src",
    "htco_platform": "packages/platform/src",
    "htco_agent_runtime": "packages/agent-runtime/src",
    "htco_integrations": "packages/integrations/src",
    "htco_api": "apps/api/src",
    "htco_worker": "workers/runner/src",
    "htco_browser_worker": "workers/browser/src",
}


@pytest.mark.parametrize(("module_name", "source_root"), WORKSPACE_PACKAGES.items())
def test_workspace_package__import__resolves_to_this_checkout(
    module_name: str, source_root: str
) -> None:
    module = importlib.import_module(module_name)

    assert module.__file__ is not None
    assert Path(module.__file__).resolve().is_relative_to(REPO_ROOT / source_root)
