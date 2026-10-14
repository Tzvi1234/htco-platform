# HTCO Testing Strategy

| | |
|---|---|
| **Applies to** | `htco-platform` (this document) and `htco-clients` (client section, detailed in that repository) |
| **Status** | Active from Phase 1 |
| **Last updated** | 2026-10-14 |

We build HTCO test-first. Tests are the executable form of the acceptance criteria, the safety net that lets AI agents change code without fear, and the evidence that a feature works. This document says which tests we write, where, with which tools, and what CI requires before anything merges.

The day-to-day procedure lives in two skills that every contributor (human or agent) follows:

- [`tdd`](../../.claude/skills/tdd/SKILL.md): the red, green, refactor loop and the edge-case checklist.
- [`feature-verification`](../../.claude/skills/feature-verification/SKILL.md): how to verify each kind of feature beyond unit tests, and how to record the evidence.

---

## 1. Principles

1. **Tests first, always.** A failing test exists before the code that makes it pass. For bugs, a failing test reproduces the bug before the fix.
2. **Test behaviour, not implementation.** Tests call public interfaces (use cases, HTTP endpoints, components) and assert outcomes. Refactoring should not break tests.
3. **Fast feedback at the bottom, real confidence at the top.** Most tests are fast and isolated; a small number exercise the real stack end to end.
4. **Fakes over mocks.** Ports get in-memory fake implementations that behave like the real thing. Mocks are only for asserting that an interaction happened (for example, "an email was requested exactly once").
5. **Don't mock what you don't own.** External SDKs and HTTP APIs are wrapped in adapters; the adapters get contract tests against recorded fixtures, and everything above them uses fakes of our own ports.
6. **Determinism.** Time, randomness, IDs and network are injected. A test that depends on the wall clock or ordering luck is a bug.
7. **Every acceptance criterion maps to a test,** and the feature's `tests.md` shows the mapping.

## 2. The TDD loop

```
1. Pick ONE acceptance criterion (Given / When / Then).
2. RED     Write the smallest test that expresses it. Run it. Watch it fail for the right reason.
3. GREEN   Write the simplest code that passes. Run that test, then the related suite.
4. REFACTOR  Improve names and structure with all tests green. Run the full suite.
5. Repeat until every criterion is covered, then add edge cases from the checklist (§7).
```

Rules that keep the loop honest, especially for AI agents:

- Run the test and read the failure before writing production code. A test that passes on first run is suspicious: check that it can fail.
- Never change an assertion to match the code. If the expectation was wrong, say so in the pull request.
- Never delete, skip or mark a failing test as expected-to-fail to get green CI without an issue that explains why.
- Keep the failing test and its fix in the same pull request, and commit the test first when practical (`test(orders): …` then `feat(orders): …`).

## 3. The test portfolio (platform)

The shape is a pyramid with a thick middle: many unit tests, a solid layer of integration and contract tests (a backend's real risk lives at its boundaries), and a thin layer of end-to-end tests.

| Level | What it covers | Tools | Runs |
|---|---|---|---|
| **Domain unit** | Entities, value objects, policies, state machines, money math. Pure, no I/O. | pytest, Hypothesis (property-based) | Every commit, under 10 s |
| **Application** | Use cases with fake ports (in-memory repositories, fake clock, fake gateway). Authorization decisions. | pytest | Every commit |
| **Persistence integration** | Repositories, transactions, outbox and inbox, queue adapter, migrations | pytest + local Supabase (Postgres) | Every pull request |
| **Database** | Row-level security per role, constraints, triggers, grants | pgTAP through `supabase test db` | Every pull request |
| **Adapter contract** | Etsy, AutoVA, email and model provider adapters against recorded fixtures: success, 4xx, 429, 5xx, timeouts, malformed payloads | pytest + respx (HTTP fixtures) | Every pull request |
| **API** | Endpoints through the ASGI app: status codes, problem+json errors, pagination, idempotency headers, authorization matrix | pytest + httpx `AsyncClient` | Every pull request |
| **API contract** | The OpenAPI document matches the implementation; no unintended breaking change; property-based fuzzing of endpoints | Schemathesis, oasdiff | Every pull request |
| **Worker and queue** | At-least-once delivery, duplicate messages, lease expiry, retries and DLQ, crash in the middle, graceful shutdown | pytest + local Postgres/pgmq | Every pull request |
| **Agent graphs** | Nodes with a fake model gateway; graph paths; interrupt and resume after restart; policy denials; injection fixtures; old-checkpoint compatibility | pytest, LangGraph in-memory and Postgres savers | Every pull request |
| **Evals** | Quality of agent outputs on gold datasets (accuracy, false-approve rate, cost, latency) | Eval harness (Phase 8) | Nightly and before promoting an agent |
| **End to end** | Critical journeys through web, API and local stack | Playwright | Every pull request for critical paths; full suite nightly |
| **Security** | Cross-tenant access, authz matrix, webhook signature and replay, secrets, dependencies, static analysis, container scan | pytest, Ruff `S` rules, gitleaks, osv-scanner, Grype; Semgrep CE weekly | Every pull request (Semgrep weekly) |
| **Resilience drills** | Kill a worker mid-task, restore a backup, provider outage | Scripts + runbooks | Before relying on production data, then quarterly |
| **Performance** | Latency budgets on hot endpoints, queue throughput | k6 or Locust (Phase 20) | Before launch and on demand |

### 3.1 What each layer must prove for common feature types

| Feature type | Must have |
|---|---|
| Domain rule or calculation | Example tests for the rule, property tests for invariants (for example "margin is never computed from floats", "total equals sum of lines"), boundary values |
| State machine | A test for every allowed transition and for at least one illegal transition from every state |
| Use case | Happy path, each failure path, authorization denied, idempotent repeat, events emitted |
| Endpoint | Contract shape, 401 and 403 cases, cross-tenant 404, validation errors as problem+json, pagination, rate-limit behaviour where relevant |
| Migration | Applies cleanly on an empty database and on a seeded copy; RLS tests for every role; previous app version still works (expand/contract) |
| Webhook or ingest | Valid signature accepted; invalid, expired and replayed signatures rejected; duplicate event is a no-op; out-of-order events converge |
| External adapter | Every documented error mapped to a typed error; timeouts; retries only on retryable errors; no secrets in logs |
| Worker | Duplicate message, crash before and after commit, lease expiry, poison message to DLQ |
| Agent capability | Allowed call succeeds; disallowed call denied and audited; budget exhausted; malicious content in inputs changes nothing; output schema enforced |

## 4. Tooling (platform)

Exact versions are pinned in `uv.lock` when Phase 1 creates it; [TOOLCHAIN.md](TOOLCHAIN.md) lists the versions chosen on 2026-10-14.

| Purpose | Tool | Notes |
|---|---|---|
| Test runner | pytest | |
| Async tests | AnyIO pytest plugin (`@pytest.mark.anyio`) | Not pytest-asyncio, whose fixture handling breaks context variables; FastAPI's own docs use AnyIO |
| HTTP API tests | httpx `AsyncClient` with `ASGITransport` | `asgi-lifespan` when startup events matter |
| Property-based | Hypothesis | Money and state-machine invariants |
| HTTP fixtures | respx | For httpx-based adapters (Etsy, Resend, providers) |
| Time control | Injected `Clock` port; time-machine where a library reads the clock | |
| Snapshots | syrupy for prompt templates (Phase 8); the OpenAPI contract is a committed file compared by `tools/openapi.py` | Never auto-updated in CI |
| Database | Supabase CLI local stack (`supabase db start`), pgTAP via `supabase test db`, basejump test helpers | Each test isolated in a rolled-back transaction |
| API contract | Schemathesis in-process on every pull request; oasdiff breaking-change check | Never pointed at production |
| Coverage | coverage.py with branch coverage | |
| Mutation | mutmut, Linux CI only | Nightly; gate is "score must not drop" on money and authorization code |
| Architecture | import-linter | Layers and forbidden-import contracts |
| Security | Ruff `S` rules, gitleaks (or its successor Betterleaks), osv-scanner on `uv.lock`, Grype or Trivy pinned by SHA for images, Semgrep CE weekly, zizmor and actionlint for workflows | All free for private repositories |

## 5. Clients (summary)

Each client keeps business rules on the server, so client tests focus on presentation logic, contracts, accessibility and platform behaviour. Details are in `htco-clients/docs/architecture/CLIENT_ARCHITECTURE.md` and that repository's skills.

| Client | Unit | Component / UI | Visual | Accessibility | End to end |
|---|---|---|---|---|---|
| Web | Vitest + Testing Library | Storybook interaction tests, Vitest browser mode | Screenshot tests (he/en × light/dark) | axe in Storybook and Playwright, keyboard-only paths | Playwright against mocked API (MSW typed from OpenAPI) and against the local stack |
| Android | JUnit + Turbine for ViewModels and flows | Compose UI tests, forced RTL | Roborazzi screenshots | TalkBack labels, touch targets ≥ 48 dp | Instrumented smoke tests |
| Windows | MSTest for ViewModels | WinUI `UITestMethod` tests | Screenshot comparisons | Narrator names, keyboard navigation, high contrast | FlaUI smoke tests |

Shared rule: every client consumes a generated API client, and a contract job regenerates all clients on every API change and fails if the result does not compile or changes unexpectedly.

## 6. Test data

- **Builders and factories** create valid domain objects with sensible defaults; tests override only what matters.
- **No production data** in tests, fixtures or seeds. Personal data in fixtures is synthetic (and obviously so).
- **Recorded HTTP fixtures** are scrubbed of tokens, emails, names and addresses before they are committed. A test fails if a fixture contains a secret-looking string.
- **Seeds** for the local stack create two organizations (each with its own shops) and one user per role, so cross-tenant tests always have a second tenant to attack.
- **Gold datasets for evals** are versioned, labeled by the owner, and contain no real buyer data.

## 7. Edge-case checklist

Before a slice is done, consider each item and either test it or note why it does not apply:

- Empty, missing, null and whitespace-only values; maximum lengths; Unicode, emoji, Hebrew and mixed-direction text.
- Boundaries: zero, one, many; first and last page; exactly at a limit and one past it.
- Duplicates and replays: the same request, event or message twice, or concurrently.
- Ordering: events out of order; a late event for an already-closed resource.
- Concurrency: two workers or two users acting on the same resource at once.
- Time: time zones, daylight-saving changes, month ends, expiries exactly at "now", clock skew on signatures.
- Money: rounding, currency mismatch, zero and negative amounts, refunds larger than the remaining balance.
- Permissions: no session, expired session, MFA level too low, wrong role, wrong shop, wrong organization.
- External failures: timeout, 429 with and without `Retry-After`, 5xx, 403 for a missing capability, malformed or partial responses.
- Partial failure: the crash happens after the database commit but before the external call, and the other way round.
- Resource limits: large payloads, many items in one order, many variants, slow consumers.
- Hostile input: injection strings, prompt-injection text in product pages or emails, oversized files, wrong content types.

## 8. CI quality gates

| Gate | From phase | Rule |
|---|---|---|
| Format, lint, type check | 1 | Zero errors |
| Unit and application tests | 1 | All pass |
| Architecture contracts | 1 | All pass (layers and forbidden imports) |
| Secret scanning | 1 | No findings |
| Dependency and SAST scans | 1 | No high or critical findings without a documented exception |
| Branch coverage, domain | 1 | ≥ 95% |
| Branch coverage, application | 1 | ≥ 90% |
| Branch coverage, whole platform | 2 | ≥ 80%, raised per phase where it makes sense |
| Test-weakening check | 1 | New skip or xfail markers, deleted tests, lowered thresholds or changed expected values need a "Test changes" section in the pull request |
| Database tests (pgTAP) | 2 | All pass; every exposed table has RLS tests |
| OpenAPI breaking-change check | 1 | No breaking change without a version bump |
| Schemathesis | 1 | No server errors or schema violations |
| Mutation score, money and authorization code | 5 | Nightly; must not drop below the last accepted score |
| Accessibility (web) | 3 | Zero critical or serious axe violations |
| Evals | 9 | Thresholds per agent capability, agreed in the feature's `tests.md` |
| Restore and kill-worker drills | 5 | Documented and passed before production data is relied on |

## 9. Conventions

- Tests live in one tree, `tests/`, with one folder per package (`tests/domain`, `tests/application`, `tests/api`, `tests/agent_runtime`, …) plus `tests/architecture`, `tests/tools` and `tests/e2e`. One tree keeps test module names unique, so a single mypy run type-checks every test.
- Warnings are errors (`filterwarnings = error`); a third-party warning is silenced only by a targeted filter with a comment. Third-party pytest plugins load only from the allow-list in `pyproject.toml`.
- Test names describe behaviour: `test_ingest_receipt__duplicate_event__is_a_no_op`.
- Acceptance criterion IDs from the feature's `overview.md` appear in test docstrings or markers (for example `@pytest.mark.ac("AC-5.4.1")`), so `tests.md` can list coverage per criterion.
- One logical assertion per test; use helpers for complex comparisons.
- No sleeping in tests; wait on conditions with timeouts.

## 10. Office acceptance scenarios

The living office ([ADR-0012](../adr/0012-living-office-organization.md)) is accepted against ten scenarios. Each is an automated test (marked `@pytest.mark.ac("SCN-<letter>")`) or, where it needs systems that do not exist yet, a scheduled test in the named phase.

| ID | Scenario | Proves | Status and location |
|---|---|---|---|
| SCN-A | **CRM observability.** An employee completes a task; identity, task, actions, costs, timestamps, outcomes and the required audit records are stored and queryable. | FR-AUD-04, FR-AUD-05 | Domain record model tested (`tests/domain/test_lifecycles.py`); end to end PLANNED for Phase 10 (stores from Phases 7–9) |
| SCN-B | **Missing business record.** An Etsy order arrives but the CRM write fails; the event stays recoverable, retries are idempotent, a discrepancy is detected, Noa receives an actionable finding and the corrected state is verified. | FR-ORD-02, FR-QOS-02 | Retry and dead-letter behaviour tested in memory (`tests/application/test_automation_engine.py`); end to end PLANNED for Phases 5 and 10 |
| SCN-C | **Nightly supervision.** A task stalls after a worker crash; monitoring detects it, the supervisor picks the right recovery and work resumes without duplicate business actions. | FR-QOS-03 | PLANNED for Phase 10 |
| SCN-D | **Optimization to automation.** Omer detects a repetitive expensive AI operation; the proposal is created, reviewed, assigned to engineering, implemented and tested, deployed in stages, and its cost and quality are measured. | FR-OPT-02 | Proposal lifecycle tested (`tests/domain/test_lifecycles.py`); end to end PLANNED for Phase 18 |
| SCN-E | **Employee-specific routing.** Two employees ask for different capabilities; both use the central router with their own policies, tool requirements, privacy rules and budgets. | FR-MDL-02 | Tested with a fake catalog (`tests/application/test_model_router.py`); with real providers PLANNED for Phase 9 |
| SCN-F | **Decision Advisor.** Rules, Jev and a fallback provider answer a structured triage task; schema validity, decision logging, quality, provider failure and the security boundary are checked. | FR-MDL-08, FR-MDL-09 | Tested with fakes (`tests/application/test_decision_advisor.py`); benchmark with real providers PLANNED for Phase 9 |
| SCN-G | **Deterministic order workflow.** The same paid order event replayed many times yields exactly one logical order, correct counters and no duplicate downstream effects. | FR-ORD-02, FR-AUT-03 | Tested in memory (`tests/application/test_automation_engine.py`); against Postgres PLANNED for Phase 5 |
| SCN-H | **Unauthorized agent action.** An employee tries a tool or automation outside its policy; the backend refuses even when the employee insists. | FR-AGT-04, FR-TOOL-02 | Tested (`tests/application/test_tool_access.py`, `test_automation_engine.py`); through the tool bridge PLANNED for Phase 8 |
| SCN-I | **Engineering safety.** An engineering employee proposes a production-changing fix; CI, permissions, review and deployment gates cannot be bypassed. | FR-ENG-03 | PLANNED for Phase 19 |
| SCN-J | **Framework independence.** An employee profile, a business task, an automation and a CRM record are defined and inspected without importing LangGraph. | FR-AGT-10 | Tested (`tests/application/test_framework_independence.py`) |

## 11. Flaky tests

A flaky test is treated as a production bug: quarantine it the same day with a linked issue, find the root cause (usually time, ordering or shared state), fix or delete it within a week. Re-running CI until it passes is not a fix.
