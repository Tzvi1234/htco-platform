# Platform skeleton — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Architecture | Layer purity (allow-list), workspace packages import from this checkout; import-linter contracts run in `just arch` | `tests/architecture/` |
| Compatibility | LangGraph two-node graph with an in-memory checkpointer on Python 3.14 | `tests/agent_runtime/test_langgraph_compat.py` |
| Tooling | Conventional Commits checker (headers, Git-generated messages, message files, titles) | `tests/tools/test_conventional_commit.py` |
| Tooling | SHA-pin checker (pinned, local and digest references pass; tags, branches, short SHAs and image tags fail) | `tests/tools/test_check_action_pins.py` |
| Tooling | Test-integrity checker (diff parsing, skip markers, removed tests, changed assertions, thresholds, quality-gate settings, the "Test changes" section) | `tests/tools/test_test_integrity.py` |
| Platform | Settings loading and errors; JSON logging (structlog and standard-library lines, request ID, exceptions, levels, quiet HTTP clients); readiness checks (pass, fail, timeout) | `tests/platform/` |
| API | Health and readiness, problem responses (404, 405, TRACE, 500, 422, streaming failure, property test over unknown paths), request IDs and access log, security headers, entry point and process exit | `tests/api/` |
| Contract | The committed contract matches the app, drift and missing-file reports, OpenAPI 3.1 with stable operation IDs, the request-ID header and problem+json on every response, deterministic rendering; Schemathesis on every operation | `tests/contract/` |
| Image policy | Base images pinned by digest, non-root runtime user, locked API-only install, allow-list build context | `tests/infra/test_dockerfile.py` |
| Image (CI) | Build, user, exit 78 without settings, read-only run answering `/healthz` and `/readyz`, Grype scan | CI `image` job |
| Agent hooks | Format hook (Python in and outside the project, other files, missing path); stop hook (pass, fail, retry with warning, skipped without code changes, output tail); `git status -z` parsing with renames; command line; settings wiring | `tests/tools/test_claude_hooks.py` |
| Observability | Trace IDs (`current_trace_id`), trace and span IDs in log lines, HTTP client defaults and `traceparent`, Sentry adapter (tags, no local variables, global client untouched, scrubbing), API end to end (one trace per request across logs and the outgoing call, forged `traceparent` ignored, diagnostic error reported without secrets, diagnostics off by default and in production, failing reporter) | `tests/platform/test_tracing.py`, `test_http.py`, `test_error_reporting.py`, `tests/api/test_observability.py`, `tests/api/test_main.py` |
| CI | The pipeline itself, on GitHub | `.github/workflows/ci.yml` (verification below) |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-1.1.1 | The whole suite through `just check` (manual run below; CI from slice 1.4) |
| AC-1.1.2 | `test_domain__imports__only_the_standard_library`, `test_application__imports__only_the_standard_library_and_the_domain`, `test_find_forbidden_imports__module_importing_fastapi__is_reported`; import-linter `dependency-rule` |
| AC-1.1.3 | `test_runtime__python_version__is_3_14`, `test_langgraph__two_node_graph_with_memory_checkpointer__runs_and_checkpoints` |
| AC-1.1.4 | The `test` recipe's per-layer `coverage report --fail-under` lines (manual run below) |
| AC-1.2.1 | `test_healthz__get__returns_ok_with_a_request_id`, `test_healthz__dependency_down__still_ok_and_never_calls_checks` |
| AC-1.2.2 | `test_readyz__configuration_valid__returns_ready`, `test_readyz__all_checks_pass__lists_each_check`, `test_readyz__check_fails__returns_503_problem_without_internals`, `test_readyz__check_hangs__fails_within_the_timeout` |
| AC-1.2.3 | `test_unknown_route__returns_404_problem_with_request_id`, `test_any_unknown_path__is_a_404_problem_never_a_500` (Hypothesis) |
| AC-1.2.4 | `test_unhandled_exception__returns_generic_500_problem`, `test_unhandled_exception__is_logged_with_stack_trace_and_the_same_request_id` |
| AC-1.2.5 | `test_load_settings__missing_required_setting__names_the_variable`, `test_main__missing_environment__exits_with_configuration_error`, `test_process__started_without_required_setting__exits_non_zero_with_a_clear_message` |
| AC-1.2.6 | `tests/platform/test_logs.py`, `test_access_log__one_json_line_per_request_with_request_id` |
| AC-1.3.1 | `test_contract__committed_file__matches_the_app`, `test_contract_drift__endpoint_changed_without_updating_the_file__is_reported` |
| AC-1.3.2 | The CI `contract` job (oasdiff); verified with the same binary locally (below) |
| AC-1.3.3 | `test_api__every_operation__conforms_to_the_contract` (Schemathesis) |
| AC-1.5.2 | `test_dockerfile__runtime_stage__runs_as_a_non_root_user`; CI `image` job "Runs as a non-root user" |
| AC-1.6.1 | `test_stop__checks_fail__blocks_and_tells_the_agent_why`, `test_stop__still_failing_after_one_retry__lets_go_but_warns_the_user`, `test_main__stop_with_failing_checks__exits_2_with_the_reason_on_stderr`, `test_settings__the_end_of_a_turn_triggers_the_check_hook` |
| AC-1.7.1 | `test_diagnostic_error__is_reported_with_ids_and_without_request_secrets`, `test_diagnostics__off_by_default__the_route_does_not_exist`, `test_main__diagnostic_endpoint__exists_only_outside_production` |
| AC-1.7.2 | `test_request__one_trace_id_in_every_log_line_and_the_outgoing_call`, `test_log_lines__inside_a_span__carry_its_trace_and_span_ids`, `test_client__inside_a_span__propagates_the_trace_as_traceparent` |
| AC-1.4.1 | The `ci-gate` job (verified on GitHub, below) |
| AC-1.4.2 | `test_main__skip_added_without_explanation__fails`, `test_main__skip_added_with_explanation__passes`, `test_diff_findings__skip_or_xfail_added_in_tests__is_reported` |
| AC-1.4.3 | `test_unpinned_uses__*`, `test_main__workflow_with_unpinned_action__fails_naming_file_and_line`; runs in the `workflows` job |

## Edge cases considered

- Import forms: `import x.y`, `from x import y`, imports inside functions and inside `TYPE_CHECKING` blocks are all reported; relative imports and `__future__` are allowed (parametrized tests).
- Nested sub-packages are scanned.
- Commit headers: empty, whitespace-only subject, missing space after the colon, empty or upper-case scope, unknown or capitalized type, exactly 100 characters (accepted) and 101 (rejected); Git-generated merge, revert, fixup and squash headers; comment lines in message files.
- Diff parsing: removed lines that start with `--` are not mistaken for file headers; tests moved between files and assertions moved unchanged are not findings; deleted test files report each test.
- "Test changes" section: missing, template comment only, `None`, `None.`, `N/A`, Windows line endings, level-3 heading.
- Action references: quoted values, reusable workflows, local actions, Docker digests vs tags, upper-case SHAs, commented-out lines.
- API input: arbitrary unknown paths (letters, digits, reserved URL characters, percent signs; property test), `TRACE`, wrong method, invalid query values (never echoed), CR/LF in an incoming `X-Request-Id` (ignored), query strings with secrets (never logged), an exception after the response started streaming (logged once, connection fails).
- Settings: missing, invalid (value never echoed), several problems at once.
- Readiness: failing, hanging (timeout) and absent checks; failures never leak the error text.
- Not applicable yet: time, money, tenancy, concurrency (no business code).

## Verification evidence

### Verification — 1.7 (part 1) — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.7.1 | `tests/api/test_observability.py` (Sentry adapter with a capturing transport) | `uv run just up`, `POST /_diagnostics/error` with `Authorization: Bearer secret-token-1` gave the 500 problem; the log has `request.failed` with the `DiagnosticError` stack trace and the same request and trace IDs as `request.completed`; the token appears nowhere in the log; `api.starting` reports `error_monitoring: off` without a DSN. Delivery to a real Sentry project is part 2. | Pass (part 1) |
| AC-1.7.2 | same, plus platform tests | Two requests got two different trace IDs; the forged `traceparent` (`0af76519…`) was ignored; every line of a request carried its trace ID. Propagation to an outgoing call is shown by the recorded request in the tests. | Pass |
| AC-1.7.3 | — | Part 2 | Open |

**Adversarial checks run:** forged `traceparent`; a reporter that raises (the response stays the 500 problem, `error_reporting.failed` is logged); cookies, bearer token and body in the failing request (absent from the captured event).
**Deployed check:** part 2.
**Open issues:** AC-1.7.3 and real Sentry delivery (part 2).

### Verification — 1.6 (part 1) — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.6.1 | `tests/tools/test_claude_hooks.py` | With a lint-clean failing test in the tree, the exact `Stop` command from `.claude/settings.json` exited 2 with `FAILED tests/tools/test_zz_red.py::test_deliberately_failing - assert 1 == 2`; repeated with `stop_hook_active: true` it exited 0 and printed the `systemMessage` warning; after removing the test it exited 0. The `PostToolUse` command reformatted a messy file and removed its unused import. | Pass |
| AC-1.6.2 | — | Needs GitHub Pro (owner task) | Open |

**Deployed check:** not applicable.
**Open issues:** the `main` ruleset (part 2).

### Verification — 1.5 (part 1) — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.5.2 | `tests/infra/test_dockerfile.py` | CI run 38495521534, job `image`: `Image user: 10001:10001`; without `HTCO_ENVIRONMENT` the container exited 78; with `--read-only --cap-drop ALL --security-opt no-new-privileges` it answered `{"status":"ok"}` and `{"status":"ready","checks":{"configuration":"ok"}}` and logged `api.starting` as JSON on `0.0.0.0:8080` | Pass |

**Adversarial checks run:** the first run failed because Docker's port proxy resets connections until the app listens (`curl: (56) Recv failure`); the probe now retries on any error. Grype v0.120.1: no high or critical findings; two medium findings in CPython 3.14.8 (CVE-2025-15367, CVE-2026-12345), fixed only in 3.15 pre-releases.
**Deployed check:** part 2.
**Open issues:** AC-1.5.1, AC-1.5.3, AC-1.5.4 (part 2).

### Verification — 1.4 — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.4.1 | `ci-gate` job | The first run of pull request #5 failed in the `image` job (a connection reset during the health probe); `ci-gate` then failed with `::error::image: failure`, so the pull request could not be green while every other job passed. The per-failure demo (failing test, lint error, type error, forbidden import, secret-like string, vulnerable dependency) is the Phase 1 demo with the owner. | Pass |
| AC-1.4.2 | `tests/tools/test_test_integrity.py` | Pull requests #3 and #4 changed quality-gate settings; `test-integrity` passed because each explained them under "Test changes". A local dry run of the same check without an explanation exited 1 listing the findings. | Pass |
| AC-1.4.3 | `tests/tools/test_check_action_pins.py` | The `workflows` job (actionlint, zizmor, SHA pins) passed on every pull request; zizmor found nothing. | Pass |

**Observed:** the first pipeline run (pull request #2) finished all seven jobs in 32 seconds; gitleaks scanned the full history ("no leaks found"); osv-scanner checked `uv.lock` ("No issues found").
**Open issues:** Renovate starts once the owner installs its GitHub App.

### Verification — 1.3 — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.3.1 | `tests/contract/test_openapi_contract.py` | Before regenerating, the old file failed `test_contract__committed_file__matches_the_app` with a diff and the `just openapi` hint | Pass |
| AC-1.3.2 | CI `contract` job | oasdiff 1.33.0 (checksum verified): removing `ReadinessStatus.checks` gave `error [response-required-property-removed] … GET /readyz`, exit 1; adding an optional field gave "No breaking changes", exit 0; identical files, exit 0 | Pass |
| AC-1.3.3 | `tests/contract/test_api_fuzzing.py` | Schemathesis ran every operation (including unsupported methods); mutation check: removing the `X-Request-Id` header in the middleware made it fail with "Missing required headers: X-Request-Id" | Pass |

**Adversarial checks run:** the header mutation above; the contract test for problem+json failed against the first generated file (FastAPI had filed the 503 schema under `application/json`) and passed after the fix.
**Deployed check:** not applicable.
**Open issues:** none.

### Verification — 1.2 — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.2.1 | `tests/api/test_health.py` | `uv run just up`, `curl -i http://127.0.0.1:8000/healthz` gave 200 `{"status":"ok"}`, `x-request-id: 01a137eb-814e-7243-…` (UUIDv7), all security headers, no `server` header | Pass |
| AC-1.2.2 | same | `curl /readyz` gave 200 `{"status":"ready","checks":{"configuration":"ok"}}`; the 503 path is covered by tests with an injected failing check (no real dependency exists yet) | Pass |
| AC-1.2.3 | `tests/api/test_errors.py` | `curl /no/such/route` gave 404 `application/problem+json` with `type …/not-found` and the request ID | Pass |
| AC-1.2.4 | same | Tests only (no failing route exists in the real app) | Pass |
| AC-1.2.5 | `tests/api/test_main.py` | `python -m htco_api` without `HTCO_ENVIRONMENT`: exit 78, `HTCO_ENVIRONMENT is required`, no traceback (subprocess test) | Pass |
| AC-1.2.6 | `tests/platform/test_logs.py` | Server output: every line JSON with `timestamp`, `level`, `event`, `request_id` (null for uvicorn startup lines, the request ID on `request.completed`) | Pass |

**Adversarial checks run:** `POST /healthz` with a forged `X-Request-Id` gave a 405 problem, `allow: GET` and a fresh server ID; `GET /healthz?token=supersecret` left no trace of the secret in the log; mutation check: removing the re-raise for failures after streaming started makes `test_exception_after_streaming_started__…` fail.
**Observed:** request `01a137eb-832a-741b-aaed-b8fda5490646` produced one `request.completed` line with method, path, status 404 and duration; no PII or secrets in the log.
**Deployed check:** not applicable (slice 1.5).
**Open issues:** none.

### Verification — 1.1 — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.1.1 | Whole suite | `uv run just check` on a fresh clone (Windows 11, uv 0.13.0, Python 3.14.8): format, lint, mypy strict clean, 4 import-linter contracts kept, 51 tests passed, coverage floors met | Pass |
| AC-1.1.2 | Purity tests | Added `import htco_api` to `htco_domain`: `just arch` failed with `Dependency rule: composition roots -> adapters -> application -> domain BROKEN` (`htco_domain -> htco_api (l.8)`). Added `import fastapi` alone: import-linter kept, purity test failed with `Broken contract: Domain imports only the standard library` / `htco_domain/__init__.py:7 imports 'fastapi'`. Both reverted. | Pass |
| AC-1.1.3 | Smoke tests | Ran on CPython 3.14.8 with langgraph 1.2.14 and langgraph-checkpoint 4.2.0 | Pass |
| AC-1.1.4 | — | Added an untested function to `htco_domain`: `just test` failed (`total of 17 is less than fail-under=80`); the domain report alone failed with `fail-under=95`. Reverted. | Pass |

**Adversarial checks run:** forbidden imports in the domain (outer layer and third-party); uncovered domain code; a commit with the message "updated some stuff" was rejected by the `commit-msg` hook; `prek run --all-files` with gitleaks found no secrets.
**Observed:** not applicable (no running service in this slice).
**Deployed check:** not applicable.
**Open issues:** none.
