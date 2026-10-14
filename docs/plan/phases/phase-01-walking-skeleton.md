# Phase 1 — Platform walking skeleton

| | |
|---|---|
| **Milestone** | M1 — Skeleton live |
| **Repositories** | `htco-platform` |
| **Size** | M |
| **Depends on** | Phase 0 (GitHub Pro, region, DigitalOcean team) |
| **Requirements** | FR-AUD-02, FR-AUD-03 |
| **Status** | In progress |

## Progress

- [x] 1.1 Workspace and tooling
- [x] 1.2 API skeleton
- [x] 1.3 Contract-first OpenAPI
- [x] 1.4 Continuous integration
- [ ] 1.5 Container and deployment
- [x] 1.6 Agent guardrails and repository rules
- [ ] 1.7 Error monitoring, tracing and alerts

Slice 1.4 was built right after 1.1 so that every later slice runs under CI; the `contract` job joined with slice 1.3 and the `image` job with slice 1.5. Slice 1.7 ships its account-free part first (tracing, trace IDs in logs, propagation, the error-reporting port with the Sentry adapter, the diagnostic endpoint); the Sentry project exists (`htco-api`, created 2026-10-14); its DSN secret and the DigitalOcean alerts follow with the deployment. Slice 1.6 shipped in two parts: the Claude Code hooks first, then the `main` ruleset on both repositories once GitHub Pro was active (2026-10-14; a direct push to `main` is rejected with GH013, AC-1.6.2). Slice 1.5 ships in two parts: the image and its CI job first, then the app spec (`infra/digitalocean/app.yaml`), registry push and owner-started deploy (`deploy.yml`), whose first run and rollback drill complete it.

## Goal

A deployed, production-shaped API that does almost nothing yet, wrapped in the full set of quality gates: workspace, architecture rules, tests, CI, security scans, container build, deployment and health checks. Everything later is built inside this frame.

## Why now

Quality gates are cheapest to add before there is code to retrofit. A walking skeleton also proves the delivery path (build, scan, deploy, roll back) before anything depends on it.

## Scope

**In:** `uv` workspace and packages, tooling (Ruff, mypy, import-linter, pytest with AnyIO, Hypothesis, coverage), FastAPI app with health, request IDs, structured logs and problem+json errors, OpenAPI contract file, CI, Dockerfile, DigitalOcean app spec with the `api` component, owner-triggered deploy workflow, Claude Code hooks, branch ruleset.

**Out:** database (Phase 2), authentication (Phase 2–3), any business endpoint.

## Owner prerequisites

- GitHub Pro active; DigitalOcean team created; region chosen.
- A DigitalOcean API token scoped for App Platform, stored as a GitHub secret by the owner.

## Slices

### 1.1 — Workspace and tooling

Create the `uv` workspace (`packages/domain`, `packages/application`, `packages/platform`, `packages/contracts`, `apps/api`), `.python-version`, Ruff, mypy strict, import-linter contracts, pytest with the AnyIO plugin, coverage gates, prek hooks and a task runner (`just`) with `check`, `check-fast` (lint, types and unit tests, used by the agent hooks), `test`, `fmt`, `lint`, `types`, `arch`.

| ID | Acceptance criterion |
|---|---|
| AC-1.1.1 | Given a clean checkout, when `just check` runs, then formatting, lint, type checks, architecture contracts and tests all run and pass. |
| AC-1.1.2 | Given `htco_domain` imports `fastapi` (in a test fixture branch), when `just arch` runs, then it fails naming the contract. |
| AC-1.1.3 | Given Python 3.14 and the pinned LangGraph version, when the smoke test builds and runs a two-node graph with an in-memory checkpointer, then it passes (closes assumption A-10; otherwise fall back to 3.13 by ADR). |
| AC-1.1.4 | Coverage below the floors (domain 95%, application 90%) fails the run. |

### 1.2 — API skeleton

FastAPI app factory in `apps/api`, settings loaded once and validated (fail fast), `GET /healthz` and `GET /readyz`, request-ID middleware, structlog JSON logging, RFC 9457 problem responses, security headers on API responses; `just up` runs the API locally.

| ID | Acceptance criterion |
|---|---|
| AC-1.2.1 | `GET /healthz` returns 200 `{"status":"ok"}` with an `X-Request-Id` header, without touching any dependency. |
| AC-1.2.2 | `GET /readyz` returns 200 when configuration is valid and 503 with a problem body when a required dependency check fails (Phase 1 checks configuration only). |
| AC-1.2.3 | An unknown route returns a problem+json 404 containing `request_id` and no stack trace. |
| AC-1.2.4 | An unhandled exception returns a problem+json 500 with `request_id`; the log line contains the stack trace and the same `request_id`; the response does not. |
| AC-1.2.5 | Starting the app with a missing required setting exits non-zero with a clear message naming the setting. |
| AC-1.2.6 | Every log line is JSON with `timestamp`, `level`, `event`, `request_id`. |

### 1.3 — Contract-first OpenAPI

`packages/contracts/openapi.yaml` generated from the app and committed; a snapshot test fails when the app's schema and the committed file differ; `oasdiff` breaking-change check against `main`; Schemathesis runs in-process.

| ID | Acceptance criterion |
|---|---|
| AC-1.3.1 | Changing an endpoint without updating `openapi.yaml` fails the contract test. |
| AC-1.3.2 | A breaking change (for example removing a response field) fails the `oasdiff` job unless the version path changes. |
| AC-1.3.3 | Schemathesis finds no server errors or schema violations on all endpoints. |

### 1.4 — Continuous integration

GitHub Actions workflow with jobs: `checks` (format, lint, types, architecture), `tests` (with coverage), `contract`, `security` (gitleaks, osv-scanner on `uv.lock`, Ruff `S`), `workflows` (zizmor, actionlint), `image` (build and scan with Grype), `test-integrity` (flags new skips, deleted tests, lowered thresholds without a "Test changes" section), `pr-title` (Conventional Commits), and a single required `ci-gate` job. All actions pinned by SHA; `permissions: {}` by default. Renovate configuration with minimum release age.

| ID | Acceptance criterion |
|---|---|
| AC-1.4.1 | A pull request with a failing test, lint error, type error, forbidden import, secret-like string or vulnerable pinned dependency cannot merge (`ci-gate` red). |
| AC-1.4.2 | A pull request that adds `@pytest.mark.skip` without a "Test changes" section fails `test-integrity`. |
| AC-1.4.3 | Every `uses:` line references a full commit SHA (checked by a script). |

### 1.5 — Container and deployment

Multi-stage Dockerfile (pinned base image by digest, non-root, bytecode compiled, `uv sync --locked --no-editable --package htco-api`), image built and scanned in CI and pushed to the registry, `infra/digitalocean/app.yaml` with the `api` service (readiness `/readyz`, liveness `/healthz`, alerts), deploy workflow started by the owner (`workflow_dispatch` or release tag) using `digitalocean/app_action/deploy@v2`.

| ID | Acceptance criterion |
|---|---|
| AC-1.5.1 | The deployed `/healthz` answers 200 over HTTPS on the app's default domain (custom domain when DNS is ready). |
| AC-1.5.2 | The container runs as a non-root user (verified in CI by inspecting the image). |
| AC-1.5.3 | A rollback to the previous deployment is performed once and documented in `docs/features/platform-skeleton/operations.md`. |
| AC-1.5.4 | No secret value appears in the app spec in git; secrets are injected at deploy time. |

### 1.6 — Agent guardrails and repository rules

`.claude/settings.json` with project hooks: format the edited Python file after edits; run `just check-fast` (lint, types, unit tests) before an agent ends its turn and block on failure. `main` ruleset: pull request required, `ci-gate` required, squash only, linear history, no force push. CLAUDE.md "Commands" section filled with the real commands.

| ID | Acceptance criterion |
|---|---|
| AC-1.6.1 | An agent session that leaves a failing unit test is told by the hook and cannot finish silently. |
| AC-1.6.2 | A direct push to `main` is rejected by GitHub. |

### 1.7 — Error monitoring, tracing and alerts

Error monitoring (Sentry or equivalent) with PII scrubbing; OpenTelemetry tracing for FastAPI and outgoing HTTP, with `trace_id` in every log line; DigitalOcean alerts (deployment failed, restarts, CPU, memory) sent to the owner. Until HTCO's own email notifications exist (Phase 6), application alerts use the error-monitoring service's alert rules.

| ID | Acceptance criterion |
|---|---|
| AC-1.7.1 | An error raised by a diagnostic endpoint (enabled only outside production) appears in error monitoring with `request_id` and `trace_id`, and without request bodies, cookies or secret headers. |
| AC-1.7.2 | A request's `trace_id` appears in all its log lines and is propagated to outgoing HTTP calls (checked against a recorded request). |
| AC-1.7.3 | A failed deployment and a crash-looping container each send one alert to the owner (exercised once on the deployed app). |

## Test plan

Unit tests for settings, middleware, error mapping; API tests through `httpx.AsyncClient`; contract snapshot; Schemathesis; image inspection test. Manual verification: call the deployed health endpoints, read the logs in DigitalOcean, roll back once (feature-verification skill).

## Security checklist

- [ ] Error responses never include stack traces or internal names (ASVS V16).
- [ ] Security headers on API responses (`X-Content-Type-Options`, `Cache-Control: no-store` on errors).
- [ ] Non-root container, pinned base image, image scan clean of high and critical findings.
- [ ] Secrets only in GitHub and DigitalOcean secret stores.
- [ ] Workflow permissions minimal; actions pinned.

## Documentation

`docs/features/platform-skeleton/` (overview, design, tests, security, operations); CLAUDE.md commands; README quickstart; TOOLCHAIN versions confirmed; ADR if Python falls back to 3.13.

## Demo

Open a pull request with a deliberately failing test and watch CI block it; fix it and merge; owner starts the deploy; open `/healthz` in a browser; roll back and forward.

## Exit gate

- [ ] All AC-1.x pass in CI and on the deployed app.
- [x] Ruleset on `main` active.
- [ ] Docs updated; this plan's status table updated.

## Rollback

DigitalOcean deployment rollback (code and config only; no data yet).

## Risks

| Risk | Mitigation |
|---|---|
| LangGraph on Python 3.14 fails | Fall back to 3.13 (ADR) before any agent code exists |
| Very new tool versions (uv 0.13, SQLAlchemy 2.1) have regressions | Pin exact versions; documented fallbacks in TOOLCHAIN |

## Cost impact

DigitalOcean `api` service about $5/month from the first deploy.
