# CLAUDE.md — htco-platform

Operating manual for AI coding agents in this repository. Read it fully at the start of every session.

## What this repository is

The backend of **HTCO OS** (HT Commerce, by H.T.Z Studios): a FastAPI API, the automation engine and the AI workforce (a LangGraph runtime behind a port), Supabase Postgres migrations, API and event contracts, DigitalOcean infrastructure as code, and the **system-level documentation** for the whole product. HTCO is a living office of 16 AI employees, deterministic automations, versioned tools and an engineering department ([ADR-0012](docs/adr/0012-living-office-organization.md)); keep the four concepts separate. The clients (web, Windows, Android, design system) live in the sibling repository `htco-clients` (checked out next to this one at `../htco-clients`).

## Talking to the owner

The owner writes in Hebrew. Answer in Hebrew in conversation. Everything committed to the repository (code, comments, docs, commit messages) is in English.

## Before any task

1. Load the `software-engineering` skill (mandatory, every task, including small fixes).
2. Load the `tdd` skill before writing or changing production code or migrations.
3. Find the current phase in [`docs/plan/PLAN.md`](docs/plan/PLAN.md) and the slice you are working on in `docs/plan/phases/`. Work only on planned slices or issues.
4. Before calling anything done: load `feature-verification`, verify for real, and record the evidence. Then load `feature-docs` and update the documentation in the same pull request.

Project hooks in `.claude/settings.json` back this up: every Python file you edit is formatted, and when code or configuration changed, the end of your turn runs `just check-fast` and is refused while it fails. Fix the reported problem; never weaken a test to get past the hook.

## Sources of truth

| Topic | Document |
|---|---|
| What we build | [`docs/product/PRD.md`](docs/product/PRD.md), [`docs/product/MVP.md`](docs/product/MVP.md) |
| Build order and status | [`docs/plan/PLAN.md`](docs/plan/PLAN.md) and phase files |
| Architecture | [`docs/architecture/SOFTWARE_DESIGN.md`](docs/architecture/SOFTWARE_DESIGN.md), data model, event catalog, permission matrix, threat model |
| Decisions | [`docs/adr/`](docs/adr/README.md) |
| Rules | [`docs/engineering/ENGINEERING_RULES.md`](docs/engineering/ENGINEERING_RULES.md) |
| Testing | [`docs/engineering/TESTING_STRATEGY.md`](docs/engineering/TESTING_STRATEGY.md) |
| Versions | [`docs/engineering/TOOLCHAIN.md`](docs/engineering/TOOLCHAIN.md) |
| Open questions | [`docs/product/ASSUMPTIONS.md`](docs/product/ASSUMPTIONS.md) |

If code and docs disagree, stop and resolve it; do not silently pick one.

## Repository layout

```text
apps/api/                 FastAPI app: routes, schemas, BFF session, composition root
workers/                  The agent-worker: automations, schedules, employee sessions; browser worker later
registry/                 The office as reviewed data: employees, automations, tools, advisors (TOML)
packages/domain/          Pure business rules (standard library only)
packages/application/     Use cases, ports, policy engine
packages/persistence/     Postgres adapters, inbox/outbox, job queue, audit
packages/integrations/    Etsy, AutoVA, email adapters
packages/model-router/    Model gateway, routing, budgets, provider SDKs
packages/agent-runtime/   LangGraph graphs and tool bridge
packages/platform/        Settings, logging, tracing, crypto, clock, IDs
packages/contracts/       openapi.yaml, event schemas
supabase/                 Migrations and pgTAP tests (single schema source)
infra/digitalocean/       App spec
tests/                    All Python tests, one folder per package (tests/api, tests/domain…) plus architecture and e2e
tools/                    Repository scripts used by hooks and CI (with tests in tests/tools)
docs/                     Product, plan, architecture, ADRs, engineering, features, cost
```

Folders appear when their first slice needs them; each `packages/<name>` is a uv workspace member whose Python package is `htco_<name>` (for example `packages/agent-runtime` → `htco_agent_runtime`).

Dependency rule (enforced in CI): `domain` ← `application` ← adapters ← `apps`/`workers`. Only the model router (`htco_model_router`) imports model SDKs; only the agent runtime (`htco_agent_runtime`) imports LangGraph; only persistence imports the database driver.

## Commands

The only prerequisite is [uv](https://docs.astral.sh/uv/); it installs Python, `just` and every tool at the versions in `uv.lock`. Run recipes as `uv run just <recipe>` (or plain `just <recipe>` with the virtual environment active). Each command is created by the slice listed below, which must also update this table. If a command does not exist yet, say so; never claim it ran.

| Command | What it runs | Created in |
|---|---|---|
| `just setup` | Install the workspace and the Git hooks (prek: ruff, gitleaks, file hygiene, Conventional Commits) | Slice 1.1 |
| `just check` | Formatting, lint, types, architecture contracts, workflow lint, tests with coverage floors (CI also scans secrets and dependencies) | Slice 1.1 |
| `just check-fast` | Lint, types and tests without coverage (used by the agent hooks) | Slice 1.1 |
| `just test` | Tests with branch coverage; fails below the floors (domain 95%, application 90%, total 80%) | Slice 1.1 |
| `just fmt` | Format and apply safe lint fixes | Slice 1.1 |
| `just lint`, `just types`, `just arch` | Ruff, mypy (strict), architecture contracts (import-linter and layer purity tests) | Slice 1.1 |
| `just workflows` | actionlint, zizmor and the SHA-pin check on `.github/` | Slice 1.4 |
| `just openapi` | Regenerate `packages/contracts/openapi.yaml` from the API (review the diff before committing) | Slice 1.3 |
| `just contract` | The committed OpenAPI matches the API; Schemathesis against it (the `oasdiff` breaking-change check runs in CI) | Slice 1.3 |
| `just registry` | Validate `registry/` (employees, automations, tools, advisors) and print a summary | ADR-0012 foundation |
| `just up` | Local API on http://127.0.0.1:8000 with `.env.example` settings (the worker joins in Phase 5) | Slice 1.2 |
| `docker build -t htco-api .` | The production API image (CI also runs it read-only and scans it) | Slice 1.5 |
| `just db-start`, `just db-reset`, `just db-test` | Local Supabase stack, migrations, pgTAP tests | Slice 2.1 |
| `just seed` | Two organizations, their shops, one user per role | Slice 2.1 |

## Golden rules

1. Tests first. Never weaken, skip or delete a test to get green.
2. Every business row is tenant-scoped; authorization is decided on the server.
3. Money is `Decimal` with a currency. Time is UTC and injected.
4. Every handler and job is idempotent; external side effects go through the outbox.
5. No secrets anywhere in the repository, logs, prompts or fixtures.
6. Model calls only through the router and gateway with the employee's policy; tools only through access checks; automations only through the engine; Rule of Two for every agent run. No employee or automation is production-ready without a passed evaluation.
7. Migrations are forward-only and tested; never applied to production by an agent.
8. Docs change in the same pull request as the code. No junk files (no session notes, scratch files or summaries).
9. Small pull requests, one slice each, Conventional Commit titles.
10. Say what you verified and how. Never claim a check you did not run.

## Ask the owner first

Production migrations or data changes; spending money; writes to Etsy, AutoVA or email with real data; permission, security or secret changes; activating an employee or automation, or changing an employee's autonomy, tools or budget; new kinds of dependencies; deviations from an ADR; deleting anything you did not create.

## Git

Trunk-based: branch from `main` (`feat/…`, `fix/…`, `docs/…`, `chore/…`), open a pull request, squash merge after green CI. Never push to `main` directly or force-push it. Commit messages follow Conventional Commits.
