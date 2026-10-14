# HTCO platform tasks. Run `just` (or `uv run just`) to list them.
# Every tool runs through `uv run`, so its version comes from uv.lock.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# List the recipes
default:
    @just --list --unsorted

# Install the workspace and the Git hooks
setup:
    uv sync --locked
    uv run prek install --install-hooks

# Format code and apply safe lint fixes
fmt:
    uv run ruff format
    uv run ruff check --fix

# Check formatting and lint rules without changing files
lint:
    uv run ruff format --check
    uv run ruff check

# Type-check everything in strict mode
types:
    uv run mypy

# Check the architecture contracts (layers and forbidden imports)
arch:
    uv run lint-imports
    uv run pytest tests/architecture -q

# Run all tests with branch coverage and the per-layer coverage floors
test:
    uv run coverage erase
    uv run coverage run -m pytest
    uv run coverage report
    uv run coverage report --include="*/htco_domain/*" --fail-under=95
    uv run coverage report --include="*/htco_application/*" --fail-under=90

# Fast feedback for agent hooks: lint, types and tests (no coverage)
check-fast: lint types
    uv run pytest -q -x

# Everything CI checks: formatting, lint, types, architecture, tests and coverage
check: lint types arch test
