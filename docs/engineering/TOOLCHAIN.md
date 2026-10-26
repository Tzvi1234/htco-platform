# Platform Toolchain

| | |
|---|---|
| **Versions checked** | 2026-10-14 (slice 1.1 locked the first set) |
| **Rule** | Exact versions live in `uv.lock`, `.python-version` and workflow files. This page records what we chose and why. Update it in the same pull request that changes a version. |
| **Release age** | `exclude-newer = "7 days"` in `pyproject.toml`: uv only resolves versions published at least seven days earlier (ER-SEC-09). The lock records the span (`P7D`), so the rule does not churn `uv.lock`. A security fix that cannot wait gets a per-package override with a comment. **Exception, uv 0.13.5 (2026-10-25):** uv itself is taken 3 days after its release (2026-10-22), not 7. Versions before 0.13.4 trust the `emSign ECC Root CA - G3`, whose intermediate keys are compromised (GHSA-hq85-5cpj-mr62, critical). 0.13.5 is the current fixed release, and its archives and image are pinned by SHA-256 and digest. |

"Locked" is the version in `uv.lock` today; "Planned" is a version this page expected before the lock existed.

## Runtime and packaging

| Area | Choice | Locked | Why |
|---|---|---|---|
| Language | CPython | 3.14.8 (`.python-version`; base image `python:3.14.x-slim-trixie` pinned by digest in slice 1.5) | 3.13 is already security-only; FastAPI, Pydantic, psycopg and pytest support 3.14. LangGraph does not declare 3.14 yet, but our smoke test runs on it (assumption A-10 verified 2026-10-14), so no fallback to 3.13 is needed. Never use floating tags (`python:3`, `slim`, `latest` now point to 3.15). |
| Packages and workspace | uv, one workspace with a virtual root and one `uv.lock` | 0.13.5, enforced with `required-version = ">=0.13.4,<0.14"` (0.13.5 since 2026-10-25: see the release-age exception below) | Fast, reproducible, workspace-aware; `--package` builds one deployable. Plain `uv sync` installs every member. |
| Build backend | `uv_build` | `>=0.13,<0.14` | Needed for non-editable installs in images |
| Task runner | just (`rust-just` from PyPI, in the `dev` group) | 1.58.0 | Comes from `uv.lock` like every other tool, so contributors only install uv |
| Images | Multi-stage `Dockerfile` from `python:3.14.8-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.5`, both pinned by digest; non-root user 10001; bytecode compiled; built with Buildx in GitHub Actions | | Built once, deployed by digest. CPython patch releases are taken when published (security fixes) and pinned by digest. |

## Application libraries

| Area | Choice | Locked / planned | Notes |
|---|---|---|---|
| HTTP API | FastAPI on Starlette, served by uvicorn | Locked FastAPI 0.142.2, Starlette 1.7.0, uvicorn 0.54.0 (no extras) | OpenAPI 3.1 output. Plain uvicorn (h11, asyncio) until measurements justify `httptools`/`uvloop`. |
| Models and validation | Pydantic | Locked 2.13.5 | Boundaries only (API, contracts, tool schemas); domain stays plain Python. mypy runs with the Pydantic plugin. |
| Settings | pydantic-settings | Locked 2.15.0 | Loaded only in composition roots through `htco_platform.settings.load_settings`; `SecretStr`; fail fast with exit code 78 and messages that never echo values |
| Database driver | psycopg 3 (binary) and `psycopg_pool` | Locked 3.3.6 and 3.3.3 (`htco-persistence`; the pool since slice 2.3) | One driver, shared with the LangGraph checkpointer; only `htco_persistence` may import it (import-linter) |
| SQL composition | Explicit SQL; SQLAlchemy 2.1 Core optional | | No ORM |
| Access-token verification | PyJWT with `cryptography` | Locked 2.15.1 / 50.0.2 (`htco-platform`) | `htco_platform.jwt` only: ES256 against the Supabase JWKS, cached 10 minutes, refreshed on an unknown key ID at most every 30 seconds |
| TOTP codes | PyOTP (MIT) | Locked 2.10.0 (`htco-integrations`) | `htco_integrations.totp` only, behind the `TotpCodes` port (ADR-0021): SHA-1 TOTP, 6 or 8 digits, server-side only |
| HTTP client | httpx | Locked 0.28.1 | Created only through `htco_platform.http.new_async_http_client`: 10-second timeout, no redirects, trace propagation. Its INFO request lines are silenced because URLs can carry credentials. |
| Logging | structlog | Locked 26.1.0 | JSON to stdout, one object per line, standard-library loggers rendered the same way; trace IDs join in slice 1.7 |
| Tracing | OpenTelemetry API and SDK, `opentelemetry-instrumentation-httpx` transport wrappers | Locked 1.45.1 / 0.66b1 | No FastAPI instrumentation: our request middleware starts the server span itself, so incoming trace headers are never trusted. No exporter yet. |
| Errors | sentry-sdk | Locked 2.71.0 | Isolated `sentry_sdk.Client` used through an isolation scope; no default or auto-enabled integrations; no local variables; `before_send` scrubbing, including every exception message not written by HTCO's own code (`htco_platform.scrubbing`, audit P1-5) |
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
| Snapshots | syrupy | Planned 6.x (Phase 8, prompt templates) | The OpenAPI contract is compared as a committed file instead (`tools/openapi.py`), so reviewers see a plain YAML diff |
| Coverage | coverage.py (`coverage run -m pytest`) | 7.16.2 | Branch coverage; floors per layer in the `test` recipe. pytest-cov is not used. |
| Mutation | mutmut | Planned 3.x (Phase 5) | Linux CI only (needs `fork`) |
| API fuzzing | Schemathesis | Locked 4.29.4 | Cases generated from the committed contract, sent in-process through Starlette's TestClient, checked with `case.validate_response` |
| Contract diff | oasdiff 1.33.0 (release binary, SHA-256 pinned in `ci.yml`) | | `oasdiff breaking` against the pull request's base; any error-level change fails the `contract` job |
| YAML | PyYAML 6.0.3 (with `types-pyyaml`) | | Renders the contract without anchors or aliases |
| HTTP mocks | respx | Planned 0.23.x | |
| Database | Supabase CLI | 2.120.0 (CI downloads the release and checks its SHA-256; locally from the GitHub release) | Local Postgres (Docker), migrations, pgTAP (`supabase test db`); the owner-started `migrate.yml` workflow for deployed projects |

## Security and supply chain

| Area | Choice | Notes |
|---|---|---|
| Secrets | gitleaks v8.30.1 (feature-frozen; Betterleaks is the successor to evaluate) | Pre-commit hook pinned by commit SHA (built with Go by prek); CI downloads the release binary and checks its SHA-256, then scans the full history |
| Dependencies | osv-scanner 2.6.0 (reads `uv.lock`), `uv audit` when stable | Every pull request; release binary checked by SHA-256 in CI. Binary versions and checksums in `ci.yml` are updated by hand (Renovate cannot update the checksum). |
| Static analysis | Ruff `S` rules on every pull request; Semgrep CE weekly | Semgrep registry rules are licensed for internal use |
| Container | Grype v0.120.1 through `anchore/scan-action` v7.4.2 (pinned by SHA) | Fails on fixable high or critical findings (`only-fixed`); unfixable base-image findings are reviewed when Renovate updates the base. Trivy's GitHub Action tags were compromised in March 2026. |
| Workflows | zizmor 1.30.1, actionlint 1.7.12 (`actionlint-py`, which verifies the binary against pinned checksums), `tools/check_action_pins.py`, `permissions: {}` by default | Installed on demand from the `ci` dependency group (`just workflows`). Actions in use: `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.2.0 (newest release older than seven days). |
| CI gate | `.github/workflows/ci.yml` with one required status, `ci-gate` | Jobs: checks, tests, security, workflows, contract (oasdiff), test-integrity (`tools/test_integrity.py`), pr-title (`tools/conventional_commit.py`); the `image` job arrives with slice 1.5 |
| Updates | Renovate (`.github/renovate.json5`: seven-day minimum release age, weekly schedule, lock-file maintenance, Actions pinned by digest); Dependabot alerts on | Needs the Renovate GitHub App installed by the owner |
| SBOM | Syft (CycloneDX) attached to releases | |

## Release

| Area | Choice | Notes |
|---|---|---|
| Commit style | Conventional Commits; PR title check; `tools/conventional_commit.py` as the `commit-msg` hook | Squash merges use the PR title. A small checker of our own replaces commitizen: same rule, no extra dependency, reused by the CI title check. |
| Versioning | release-please, one root component, `release-type: simple` | Workspace member versions stay fixed at `0.0.0` so `uv.lock` does not churn |
| Deploy | `digitalocean/app_action/deploy@v2`, pinned by SHA | Owner-triggered for production |
