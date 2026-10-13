# Platform Toolchain

| | |
|---|---|
| **Versions checked** | 2026-10-14 |
| **Rule** | Exact versions live in `uv.lock`, `.python-version` and workflow files. This page records what we chose and why. Update it in the same pull request that changes a version. |

## Runtime and packaging

| Area | Choice | Version | Why |
|---|---|---|---|
| Language | CPython | 3.14.x (base image `python:3.14.x-slim-trixie` pinned by digest) | 3.13 is already security-only; FastAPI, Pydantic, psycopg and pytest support 3.14. LangGraph runs on 3.14 but does not declare it yet, so Phase 1 runs a LangGraph smoke test on 3.14; fallback is 3.13. Never use floating tags (`python:3`, `slim`, `latest` now point to 3.15). |
| Packages and workspace | uv (one workspace, one `uv.lock`) | 0.13.x pinned with `required-version`; fallback 0.12.24 | Fast, reproducible, workspace-aware; `--package` builds one deployable |
| Build backend | `uv_build` | `>=0.13,<0.14` | Needed for non-editable installs in images |
| Images | Multi-stage Dockerfile, non-root user, bytecode compiled, built with Buildx in GitHub Actions | | Built once, deployed by digest |

## Application libraries

| Area | Choice | Version | Notes |
|---|---|---|---|
| HTTP API | FastAPI | 0.143.x | OpenAPI 3.1 output |
| Models and validation | Pydantic | 2.14.x | Boundaries only (API, contracts, tool schemas); domain stays plain Python |
| Settings | pydantic-settings | 2.15.x | Loaded only in composition roots; `SecretStr`; fail fast |
| Database driver | psycopg 3 with `psycopg_pool` | 3.3.x | One driver, shared with the LangGraph checkpointer |
| SQL composition | Explicit SQL; SQLAlchemy 2.1 Core optional | 2.1.x | No ORM |
| HTTP client | httpx | | Async, timeouts everywhere |
| Logging | structlog | 26.x | JSON to stdout with trace and correlation IDs |
| Tracing | OpenTelemetry SDK + FastAPI instrumentation | 1.45.x / 0.66bX | |
| Errors | sentry-sdk | 2.x | PII scrubbing on |
| Agent runtime | langgraph, langgraph-checkpoint (≥ 4.3), langgraph-checkpoint-postgres | 1.2.x / 3.1.x | Strict msgpack, encrypted serializer; checkpoint tables created by a migration |
| Model providers | `anthropic`, `openai` official SDKs | | Only inside `htco_model_router` |
| Email | `resend` | 2.49.x | Only inside `htco_integrations.email` |

## Quality and testing

| Area | Choice | Version | Notes |
|---|---|---|---|
| Lint and format | Ruff | 0.17.x | Rule sets: E, W, F, I, UP, B, SIM, C4, PT, RUF, S, ASYNC, FAST, TID, ARG, DTZ, T20, ERA, PL, PERF, FURB, N, TRY, LOG, G, RET, PTH |
| Type checking | mypy `--strict` with the Pydantic plugin (gate); ty (advisory) | 2.4.x / 0.0.x | Revisit ty when it reaches 1.0 |
| Architecture rules | import-linter | 2.15.x | Layers and forbidden-import contracts |
| Git hooks | prek (pre-commit compatible) | 0.5.x | Hooks call `uv run …` so versions come from `uv.lock`; CI re-runs every hook |
| Tests | pytest + AnyIO plugin | 9.1.x / 4.15.x | No pytest-asyncio |
| Property-based | Hypothesis | 6.x | |
| Snapshots | syrupy | 6.x | |
| Coverage | coverage.py, pytest-cov | 7.x | Branch coverage |
| Mutation | mutmut | 3.x | Linux CI only (needs `fork`) |
| API fuzzing | Schemathesis | 4.x | In-process on pull requests |
| HTTP mocks | respx | 0.23.x | |
| Database | Supabase CLI | 2.120.x | Local stack, migrations, pgTAP |

## Security and supply chain

| Area | Choice | Notes |
|---|---|---|
| Secrets | gitleaks (feature-frozen) or Betterleaks | Pre-commit hook and CI |
| Dependencies | osv-scanner (reads `uv.lock`), `uv audit` when stable | Every pull request |
| Static analysis | Ruff `S` rules on every pull request; Semgrep CE weekly | Semgrep registry rules are licensed for internal use |
| Container | Grype, or Trivy pinned by full commit SHA | Trivy's GitHub Action tags were compromised in March 2026 |
| Workflows | zizmor, actionlint, SHA-pinned actions, `permissions: {}` by default | |
| Updates | Renovate (one config for Python, npm, Gradle, NuGet, Docker, Actions) with a 3–7 day minimum release age; Dependabot alerts on | |
| SBOM | Syft (CycloneDX) attached to releases | |

## Release

| Area | Choice | Notes |
|---|---|---|
| Commit style | Conventional Commits; PR title check; commitizen `commit-msg` hook | Squash merges use the PR title |
| Versioning | release-please, one root component, `release-type: simple` | Workspace member versions stay fixed so `uv.lock` does not churn |
| Deploy | `digitalocean/app_action/deploy@v2`, pinned by SHA | Owner-triggered for production |
