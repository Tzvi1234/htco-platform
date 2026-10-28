# HTCO platform tasks. Run `just` (or `uv run just`) to list them.
# Every tool runs through `uv run`, so its version comes from uv.lock.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# The local Supabase database (`just db-start`); CI and other setups override it.
export HTCO_TEST_DATABASE_URL := env_var_or_default("HTCO_TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

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

# Run all tests except the API fuzzing (see `fuzz`) with branch coverage and the per-layer coverage floors
test:
    uv run coverage erase
    uv run coverage run -m pytest --ignore=tests/contract/test_api_fuzzing.py
    uv run coverage report
    uv run coverage report --include="*/htco_domain/*" --fail-under=95
    uv run coverage report --include="*/htco_application/*" --fail-under=90

# Schemathesis API fuzzing (the slow part of the contract tests; its own CI job so it runs in parallel with `test`).
# No argument fuzzes every operation; CI passes `INDEX/COUNT` (e.g. 0/4) for one stable crc32 shard.
fuzz shard='':
    uv run pytest tests/contract/test_api_fuzzing.py -q {{ if shard == '' { '' } else { '--fuzz-shard=' + shard } }}

# Local database (needs Docker and the Supabase CLI): start Postgres and apply every migration
db-start:
    supabase db start

# Stop the local Supabase stack
db-stop:
    supabase stop

# Drop the local database and re-apply every migration and the seed
db-reset:
    supabase db reset

# Local data: two organizations, their shops, one user per role (supabase/seed.sql, via a reset)
seed: db-reset

# Database tests (pgTAP in supabase/tests)
db-test:
    supabase test db

# Integration tests against the local database (after `just db-start`)
test-db:
    uv run pytest -m db tests/integration

# Run the API locally on http://127.0.0.1:8000 with the settings in .env.example
up:
    uv run --env-file .env.example python -m htco_api

# Fast feedback for agent hooks: lint, types and tests (no coverage)
check-fast: lint types
    uv run pytest -q -x -m "not db and not slow"

# What CI checks, minus the secret and vulnerability scans: format, lint, types, contracts, workflows, tests
check: lint types arch workflows test fuzz
