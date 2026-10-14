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

# Lint the GitHub workflows: actionlint, zizmor and SHA-pinned actions
workflows:
    uv run --group ci actionlint
    uv run --group ci zizmor --min-severity low .github
    uv run python -m tools.check_action_pins

# Validate the office registry (employees, tools, automations, advisors) and print a summary
registry:
    uv run python -m tools.registry --check

# Regenerate packages/contracts/openapi.yaml from the API; review the diff before committing
openapi:
    uv run python -m tools.openapi --write

# Contract checks: the committed OpenAPI matches the API and Schemathesis finds no violations
# (the breaking-change check, oasdiff, runs in CI against the pull request's base)
contract:
    uv run python -m tools.openapi --check
    uv run pytest tests/contract -q

# The Supabase CLI at the version in docs/engineering/TOOLCHAIN.md (minimum release age applies)
supabase := "npx --yes supabase@2.118.0"
# Postgres of the local stack; the CLI's fixed local credentials, never used anywhere else
local_database_url := "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

# Start the local Supabase stack (needs Docker) and apply every migration
db-start:
    {{supabase}} start
    @echo "Database tests: set HTCO_TEST_DATABASE_URL={{local_database_url}}"

# Stop the local Supabase stack
db-stop:
    {{supabase}} stop

# Recreate the local database from the migrations (and the seed, from slice 2.2)
db-reset:
    {{supabase}} db reset

# Database tests: pgTAP in supabase/tests, then the Python tests marked `database`
db-test $HTCO_TEST_DATABASE_URL=local_database_url:
    {{supabase}} test db
    uv run pytest -m database -q

# Run all tests with branch coverage and the per-layer coverage floors
test:
    uv run coverage erase
    uv run coverage run -m pytest
    uv run coverage report
    uv run coverage report --include="*/htco_domain/*" --fail-under=95
    uv run coverage report --include="*/htco_application/*" --fail-under=90

# Run the API locally on http://127.0.0.1:8000 with the settings in .env.example
up:
    uv run --env-file .env.example python -m htco_api

# Fast feedback for agent hooks: lint, types and tests (no coverage)
check-fast: lint types
    uv run pytest -q -x

# What CI checks, minus the secret and vulnerability scans: format, lint, types, contracts, workflows, tests
check: lint types arch workflows test
