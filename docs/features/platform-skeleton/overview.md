# Platform skeleton — Overview

| | |
|---|---|
| **Slug** | `platform-skeleton` |
| **Phase / slices** | Phase 1 — slices 1.1 to 1.7 |
| **Requirements** | [FR-AUD-02, FR-AUD-03](../../product/PRD.md) |
| **Status** | In progress |
| **Last updated** | 2026-10-14 |

## What and why

The frame every later feature is built inside: a uv workspace whose packages follow the hexagonal layers, the quality gates that keep it that way (formatting, lint, strict types, architecture contracts, tests with coverage floors, secret scanning, commit rules), and, in later slices, a production-shaped API with health checks, CI, a container and a deployment. Adding gates before there is code is cheaper than retrofitting them.

## Users and actors

- **Developers and AI coding agents**, who get fast local feedback (`just check-fast`, Git hooks) and the same checks CI runs (`just check`).
- **The owner**, who reviews pull requests knowing every gate has run, and starts production deploys.
- **DigitalOcean health checks**, which call `/healthz` and `/readyz` (slice 1.2).

## Acceptance criteria

| ID | Criterion | Slice | Status |
|---|---|---|---|
| AC-1.1.1 | Given a clean checkout, when `just check` runs, then formatting, lint, type checks, architecture contracts and tests all run and pass. | 1.1 | Pass |
| AC-1.1.2 | Given `htco_domain` imports `fastapi`, when `just arch` runs, then it fails naming the contract. | 1.1 | Pass |
| AC-1.1.3 | Given Python 3.14 and the pinned LangGraph version, when the smoke test builds and runs a two-node graph with an in-memory checkpointer, then it passes. | 1.1 | Pass |
| AC-1.1.4 | Coverage below the floors (domain 95%, application 90%) fails the run. | 1.1 | Pass |
| AC-1.2.1 | `GET /healthz` returns 200 `{"status":"ok"}` with an `X-Request-Id` header, without touching any dependency. | 1.2 | Pass |
| AC-1.2.2 | `GET /readyz` returns 200 when configuration is valid and 503 with a problem body when a required dependency check fails (Phase 1 checks configuration only). | 1.2 | Pass |
| AC-1.2.3 | An unknown route returns a problem+json 404 containing `request_id` and no stack trace. | 1.2 | Pass |
| AC-1.2.4 | An unhandled exception returns a problem+json 500 with `request_id`; the log line contains the stack trace and the same `request_id`; the response does not. | 1.2 | Pass |
| AC-1.2.5 | Starting the app with a missing required setting exits non-zero with a clear message naming the setting. | 1.2 | Pass |
| AC-1.2.6 | Every log line is JSON with `timestamp`, `level`, `event`, `request_id`. | 1.2 | Pass |
| AC-1.3.1 | Changing an endpoint without updating `openapi.yaml` fails the contract test. | 1.3 | Pass |
| AC-1.3.2 | A breaking change (for example removing a response field) fails the `oasdiff` job unless the version path changes. | 1.3 | Pass |
| AC-1.3.3 | Schemathesis finds no server errors or schema violations on all endpoints. | 1.3 | Pass |
| AC-1.5.1 | The deployed `/healthz` answers 200 over HTTPS on the app's default domain. | 1.5 (part 2) | Pass (2026-10-14, `https://htco-s4o93.ondigitalocean.app`; see Drills in operations.md) |
| AC-1.5.2 | The container runs as a non-root user (verified in CI by inspecting the image). | 1.5 | Pass |
| AC-1.5.3 | A rollback to the previous deployment is performed once and documented in `operations.md`. | 1.5 (part 2) | Pass (2026-10-14, `https://htco-s4o93.ondigitalocean.app`; see Drills in operations.md) |
| AC-1.5.4 | No secret value appears in the app spec in git; secrets are injected at deploy time. | 1.5 (part 2) | Pass (`tests/infra/test_app_spec.py`) |
| AC-1.6.1 | An agent session that leaves a failing unit test is told by the hook and cannot finish silently. | 1.6 | Pass |
| AC-1.6.2 | A direct push to `main` is rejected by GitHub. | 1.6 (part 2, needs GitHub Pro) | Open |
| AC-1.7.1 | An error raised by a diagnostic endpoint (enabled only outside production) appears in error monitoring with `request_id` and `trace_id`, and without request bodies, cookies or secret headers. | 1.7 | Pass in tests with a capturing transport; the real Sentry project is part 2 |
| AC-1.7.2 | A request's `trace_id` appears in all its log lines and is propagated to outgoing HTTP calls (checked against a recorded request). | 1.7 | Pass |
| AC-1.7.3 | A failed deployment and a crash-looping container each send one alert to the owner. | 1.7 (part 2, with the deployment) | Open |
| AC-1.4.1 | A pull request with a failing test, lint error, type error, forbidden import, secret-like string or vulnerable pinned dependency cannot merge (`ci-gate` red). | 1.4 | Pass |
| AC-1.4.2 | A pull request that adds `@pytest.mark.skip` without a "Test changes" section fails `test-integrity`. | 1.4 | Pass |
| AC-1.4.3 | Every `uses:` line references a full commit SHA (checked by a script). | 1.4 | Pass |

Slices 1.2 to 1.7 add their criteria here as they start; the full list is in the [phase plan](../../plan/phases/phase-01-walking-skeleton.md).

## Scope

- **In (1.1):** workspace and packages, Ruff, mypy, import-linter and the layer purity test, pytest with AnyIO and Hypothesis, coverage floors, `just` recipes, prek Git hooks (ruff, gitleaks, file hygiene, Conventional Commits).
- **In (1.2):** the FastAPI app factory, `/healthz` and `/readyz`, settings validated at startup, request IDs, JSON logging, RFC 9457 problem responses, security headers, `just up`.
- **In (1.3):** `packages/contracts/openapi.yaml` generated from the API and committed, a drift test, Schemathesis against the contract, and the `oasdiff` breaking-change job.
- **In (1.5, part 1):** the production image (`Dockerfile`, `.dockerignore`) and the CI `image` job.
- **In (1.6, part 1):** Claude Code project hooks: format edited Python files; refuse to end a turn while `just check-fast` fails.
- **In (1.7, part 1):** a server span per request, `trace_id` in every log line, trace propagation through `htco_platform.http`, the `ErrorReporter` port with the Sentry adapter, and `POST /_diagnostics/error` outside production.
- **In (1.4):** the CI workflow with one required status (`ci-gate`), secret and dependency scans, workflow linting, the test-weakening check, the pull request title check, and the Renovate configuration.
- **Out (so far):** the app spec, registry and deployment (1.5, part 2), the branch ruleset (1.6, part 2), the Sentry project and DigitalOcean alerts (1.7, part 2).

## External facts learned

- 2026-10-14: LangGraph 1.2.14 with langgraph-checkpoint 4.2.0 runs a checkpointed two-node graph on CPython 3.14.8, although its package metadata only declares up to 3.13 (assumption A-10 verified).
- 2026-10-14: `langsmith`, which LangGraph pulls in through `langchain-core`, calls `asyncio.iscoroutinefunction`, deprecated in Python 3.14 and due for removal in 3.16. It is silenced by one targeted warning filter; the dependency must be fixed upstream before we move to Python 3.16.
- 2026-10-14: `langsmith` registers a pytest plugin through entry points. Our pytest run loads only allow-listed plugins.
- 2026-10-14: httpx logs every request URL, query string included, at INFO. Query strings can carry API keys or OAuth codes, so `configure_logging` keeps the `httpx` and `httpcore` loggers at WARNING.
- 2026-10-14: uvicorn sends a `server: uvicorn` header unless started with `server_header=False`.
- 2026-10-14: FastAPI documents an error response's model under the route's media type (`application/json`) even when the response declares `application/problem+json`; `HtcoAPI.openapi` moves it.
- 2026-10-14: Schemathesis's own ASGI client keeps the app's lifespan running in a background portal until the interpreter exits, which leaks memory streams that our strict warning settings turn into errors. Our fuzz test sends each case through a TestClient it closes, and validates the response with `case.validate_response`.
- 2026-10-14: httpx deprecates per-request cookies; cookies go on the client.
- 2026-10-14: `sentry_sdk.Scope.capture_exception` on a scope you create yourself ignores that scope's client (Sentry looks the client up from the context scopes). Capturing inside `sentry_sdk.isolation_scope()` after `scope.set_client(...)` works and leaves the process-wide client inactive.
- 2026-10-14: Sentry events carry source-code context lines around each frame even without local variables, so secrets must never be literals in code (they are not, by rule).
- 2026-10-14: uv records a relative `exclude-newer` as a span in `uv.lock` (`exclude-newer-span = "P7D"`), so the release-age rule does not make the lock stale.

## Known limits

- The domain and application packages contain no business code yet; their coverage floors start to bite with Phase 2.
- The import-linter contracts list only the packages that exist; each new package adds itself to the contracts in the slice that creates it.
