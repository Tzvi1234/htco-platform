# Platform Toolchain

| | |
|---|---|
| **Versions checked** | 2026-10-14 (slice 1.1 locked the first set) |
| **Rule** | Exact versions live in `uv.lock`, `.python-version` and workflow files. This page records what we chose and why. Update it in the same pull request that changes a version. |
| **Release age** | `exclude-newer = "7 days"` in `pyproject.toml`: uv only resolves versions published at least seven days earlier (ER-SEC-09). The lock records the span (`P7D`), so the rule does not churn `uv.lock`. A security fix that cannot wait gets a per-package override with a comment. |

"Locked" is the version in `uv.lock` today; "Planned" is a version this page expected before the lock existed.

## Runtime and packaging

| Area | Choice | Locked | Why |
|---|---|---|---|
| Language | CPython | 3.14.8 (`.python-version`; base image `python:3.14.x-slim-trixie` pinned by digest in slice 1.5) | 3.13 is already security-only; FastAPI, Pydantic, psycopg and pytest support 3.14. LangGraph does not declare 3.14 yet, but our smoke test runs on it (assumption A-10 verified 2026-10-14), so no fallback to 3.13 is needed. Never use floating tags (`python:3`, `slim`, `latest` now point to 3.15). |
| Packages and workspace | uv, one workspace with a virtual root and one `uv.lock` | 0.13.0, enforced with `required-version = ">=0.13,<0.14"` | Fast, reproducible, workspace-aware; `--package` builds one deployable. Plain `uv sync` installs every member. |
| Build backend | `uv_build` | `>=0.13,<0.14` | Needed for non-editable installs in images |
| Task runner | just (`rust-just` from PyPI, in the `dev` group) | 1.58.0 | Comes from `uv.lock` like every other tool, so contributors only install uv |
| Images | Multi-stage Dockerfile, non-root user, bytecode compiled, built with Buildx in GitHub Actions | | Built once, deployed by digest (slice 1.5) |

## Application libraries

| Area | Choice | Locked / planned | Notes |
|---|---|---|---|
| HTTP API | FastAPI | Planned 0.143.x (slice 1.2) | OpenAPI 3.1 output |
| Models and validation | Pydantic | Locked 2.13.5 (pulled in by LangGraph) | Boundaries only (API, contracts, tool schemas); domain stays plain Python |
| Settings | pydantic-settings | Planned 2.15.x (slice 1.2) | Loaded only in composition roots; `SecretStr`; fail fast |
| Database driver | psycopg 3 with `psycopg_pool` | Planned 3.3.x (Phase 2) | One driver, shared with the LangGraph checkpointer |
| SQL composition | Explicit SQL; SQLAlchemy 2.1 Core optional | | No ORM |
| HTTP client | httpx | | Async, timeouts everywhere |
| Logging | structlog | Planned 26.x (slice 1.2) | JSON to stdout with trace and correlation IDs |
| Tracing | OpenTelemetry SDK + FastAPI instrumentation | Planned 1.45.x / 0.66bX (slice 1.7) | |
| Errors | sentry-sdk | Planned 2.x (slice 1.7) | PII scrubbing on |
| Agent runtime | langgraph, langgraph-checkpoint, langgraph-checkpoint-postgres | Locked langgraph 1.2.14, langgraph-checkpoint 4.2.0 (langchain-core 1.6.7) | Only in `htco_agent_runtime`. Checkpoint 4.3.0 (store namespace validation) is inside the release-age window today and is taken before Phase 8 adds agent code. Strict msgpack, encrypted serializer; checkpoint tables created by a migration. |
| Model providers | `anthropic`, `openai` official SDKs | | Only inside `htco_model_router` |
| Email | `resend` | 2.49.x | Only inside `htco_integrations.email` |

## Quality and testing

| Area | Choice | Locked | Notes |
|---|---|---|---|
| Lint and format | Ruff | 0.16.10 | Rule sets: E, W, F, I, UP, B, SIM, C4, PT, RUF, S, ASYNC, FAST, TID, ARG, DTZ, T20, ERA, PL, PERF, FURB, N, TRY, LOG, G, RET, PTH. 0.17 arrives through Renovate once it is a week old. |
| Type checking | mypy `--strict` (gate) with extra error codes; ty (advisory, not wired yet) | 2.4.0 | One run over `packages`, `apps`, `tests` and `tools`; the Pydantic plugin is added with the first Pydantic code. Revisit ty when it reaches 1.0. |
| Architecture rules | import-linter, plus `tests/architecture` for the allow-list rule | 2.15 | Layers and forbidden-import contracts; domain and application purity is an AST test because import-linter has no allow-list contract |
| Git hooks | prek (pre-commit compatible), in the `dev` group | 0.5.5 | Ruff, gitleaks, file hygiene and the commit-message check; CI re-runs every check |
| Tests | pytest + AnyIO plugin | 9.1.1 / 4.15.1 | No pytest-asyncio. Third-party plugins load only from an allow-list (`--disable-plugin-autoload -p anyio -p hypothesispytest`); warnings are errors. |
| Property-based | Hypothesis | 6.168.5 | |
| Snapshots | syrupy | Planned 6.x (slice 1.3) | |
| Coverage | coverage.py (`coverage run -m pytest`) | 7.16.2 | Branch coverage; floors per layer in the `test` recipe. pytest-cov is not used. |
| Mutation | mutmut | Planned 3.x (Phase 5) | Linux CI only (needs `fork`) |
| API fuzzing | Schemathesis | Planned 4.x (slice 1.3) | In-process on pull requests |
| HTTP mocks | respx | Planned 0.23.x | |
| Database | Supabase CLI | Planned 2.120.x (slice 2.1) | Local stack, migrations, pgTAP |

## Security and supply chain

| Area | Choice | Notes |
|---|---|---|
| Secrets | gitleaks v8.30.1 (feature-frozen; Betterleaks is the successor to evaluate) | Pre-commit hook pinned by commit SHA (built with Go by prek); CI downloads the release binary and checks its SHA-256, then scans the full history |
| Dependencies | osv-scanner 2.6.0 (reads `uv.lock`), `uv audit` when stable | Every pull request; release binary checked by SHA-256 in CI. Binary versions and checksums in `ci.yml` are updated by hand (Renovate cannot update the checksum). |
| Static analysis | Ruff `S` rules on every pull request; Semgrep CE weekly | Semgrep registry rules are licensed for internal use |
| Container | Grype, or Trivy pinned by full commit SHA | Trivy's GitHub Action tags were compromised in March 2026 |
| Workflows | zizmor 1.30.1, actionlint 1.7.12 (`actionlint-py`, which verifies the binary against pinned checksums), `tools/check_action_pins.py`, `permissions: {}` by default | Installed on demand from the `ci` dependency group (`just workflows`). Actions in use: `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.2.0 (newest release older than seven days). |
| CI gate | `.github/workflows/ci.yml` with one required status, `ci-gate` | Jobs: checks, tests, security, workflows, test-integrity (`tools/test_integrity.py`), pr-title (`tools/conventional_commit.py`); `contract` and `image` jobs arrive with slices 1.3 and 1.5 |
| Updates | Renovate (`.github/renovate.json5`: seven-day minimum release age, weekly schedule, lock-file maintenance, Actions pinned by digest); Dependabot alerts on | Needs the Renovate GitHub App installed by the owner |
| SBOM | Syft (CycloneDX) attached to releases | |

## Release

| Area | Choice | Notes |
|---|---|---|
| Commit style | Conventional Commits; PR title check; `tools/conventional_commit.py` as the `commit-msg` hook | Squash merges use the PR title. A small checker of our own replaces commitizen: same rule, no extra dependency, reused by the CI title check. |
| Versioning | release-please, one root component, `release-type: simple` | Workspace member versions stay fixed at `0.0.0` so `uv.lock` does not churn |
| Deploy | `digitalocean/app_action/deploy@v2`, pinned by SHA | Owner-triggered for production |
