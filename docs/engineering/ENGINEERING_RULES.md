# HTCO Engineering Rules

| | |
|---|---|
| **Applies to** | `htco-platform` and `htco-clients`, every contributor, human or AI |
| **Status** | Active from Phase 0 |
| **Last updated** | 2026-10-14 |

These are the rules we keep for the whole life of the project. Each rule has an ID so reviews and commits can cite it ("violates ER-SEC-04"). A rule changes only through a pull request that updates this file and, for anything structural, an ADR.

Client-specific rules (MVVM, Compose, WinUI, web) live in `htco-clients/docs/architecture/CLIENT_ARCHITECTURE.md` and extend this document; they never contradict it.

---

## 1. The way we build

Every change, however small, follows the same loop:

```
Issue ─► Design note / ADR (if needed) ─► Acceptance criteria ─► Failing tests ─► Minimal code ─► Refactor
  ─► Verify for real ─► Docs ─► Pull request ─► Review + CI ─► Merge ─► Deploy ─► Verify in production
```

- **ER-WAY-01** No code without a reason recorded somewhere: an issue, a phase slice or a bug report.
- **ER-WAY-02** Acceptance criteria are written before tests, as Given/When/Then, and each one maps to at least one test.
- **ER-WAY-03** Tests are written before the code that makes them pass (see [TESTING_STRATEGY.md](TESTING_STRATEGY.md)).
- **ER-WAY-04** Work moves in vertical slices: a thin path through API, data and UI that a user can exercise, rather than a finished layer nobody can use.
- **ER-WAY-05** A change is done when the [Definition of Done](#15-definition-of-ready-and-definition-of-done) holds, not when the code compiles.

## 2. Principles

We use these as tools for judgment, not as goals in themselves.

| Principle | What it means here |
|---|---|
| **Correct and safe before fast** | Money, permissions, external writes and customer data get the most tests and the slowest review. |
| **KISS** | The simplest design that meets today's acceptance criteria and keeps tomorrow's options open. |
| **YAGNI** | No abstraction, flag, config option or plugin point until a real second use case exists. |
| **DRY (rule of three)** | Duplicate knowledge is a bug; duplicate *code* is tolerated until the third occurrence. Never merge two things that only look alike. |
| **SOLID where it helps** | Single responsibility and dependency inversion matter most to us (ports and adapters). We do not build class hierarchies to satisfy a principle. |
| **Composition over inheritance** | Small objects and functions combined explicitly. |
| **Functional core, imperative shell** | Business rules are pure functions over plain data; I/O happens at the edges. |
| **Make illegal states unrepresentable** | Value objects, enums and explicit state machines instead of loose strings and booleans. |
| **Explicit over implicit** | No hidden globals, no import-time side effects, no magic registration. Dependencies are passed in. |
| **Boring technology** | Prefer mature, well-documented tools. A new technology needs an ADR. |

## 3. Architecture (ER-ARCH)

- **ER-ARCH-01 Layers and dependency direction.** `domain` depends on nothing outside the standard library. `application` (use cases and ports) depends on `domain`. Adapters (persistence, integrations, model gateway, agent runtime) implement ports and depend inward. Transport (HTTP, queue consumers, schedulers) and composition roots wire everything together. Nothing inward ever imports outward. CI enforces this with architecture tests.
- **ER-ARCH-02 Ports and adapters.** Every external system (database, Etsy, AutoVA, email, model providers, clock, randomness, file storage) is reached through a port interface owned by the application layer. Swapping an adapter never touches domain code.
- **ER-ARCH-03 Bounded contexts.** Identity & Access, Shops, Catalog, Sourcing, Orders & Fulfillment, Finance, Support, AI Workforce, Observability & Incidents, Media & Marketing. A context talks to another through its application API or through events, never by reading the other context's tables.
- **ER-ARCH-04 No business logic in transport or UI.** Routers, consumers, view models and screens translate and delegate. Rules such as pricing, order transitions and permissions live in the domain and are enforced on the server only.
- **ER-ARCH-05 Contracts first.** API changes start in the OpenAPI document; event changes start in the event schema. Clients are generated from contracts. Breaking changes need a new version.
- **ER-ARCH-06 No persistence models across boundaries.** Database rows, ORM models and provider payloads never leave their adapter. Map them to domain types or contract DTOs.
- **ER-ARCH-07 Modular monolith first.** One deployable API and a small number of workers. A new service needs an ADR showing why a module is not enough.
- **ER-ARCH-08 Agents are clients of the domain.** An agent never gets database credentials, the Supabase service role key or raw HTTP access. It asks for tools; domain services execute them after a policy check.

## 4. Code (ER-CODE)

- **ER-CODE-01 Formatting and linting are automated.** Ruff (Python), ESLint + Prettier (TypeScript), ktlint/detekt (Kotlin), `dotnet format` and analyzers (C#). Nobody argues about style in review.
- **ER-CODE-02 Strict typing.** Python is fully type-checked in strict mode; TypeScript `strict: true` with no `any` without a comment explaining why; Kotlin with explicit API mode in shared modules; C# with nullable reference types enabled and warnings as errors.
- **ER-CODE-03 Names tell the domain story.** Use the [glossary](../product/GLOSSARY.md). A function name says what it does (`ingest_receipt`), a boolean reads as a question (`is_expired`), no abbreviations except well-known ones (`id`, `url`, `sku`).
- **ER-CODE-04 Small units.** A function does one thing and fits on a screen. A module has one reason to change. If you need "and" to describe it, split it.
- **ER-CODE-05 Errors are typed and handled at the right level.** Domain errors are explicit types. No bare `except`/`catch (Exception)` except at process boundaries, where the error is logged with context and converted to a problem response or a task failure. Never swallow an error silently.
- **ER-CODE-06 API errors use one envelope.** RFC 9457 `application/problem+json` with a stable `type`, a `request_id` and no internal details.
- **ER-CODE-07 Configuration is validated at startup.** Settings come from environment variables through one typed settings object. A missing or invalid setting stops the process at boot, not at the first request.
- **ER-CODE-08 Time, randomness and IDs are injected.** Code that needs "now" or a new ID receives a clock or ID generator, so tests are deterministic.
- **ER-CODE-09 Every new dependency is justified.** The pull request says why, checks the license, pins the version and confirms it is maintained. Prefer the standard library and existing dependencies.
- **ER-CODE-10 Comments explain why, not what.** Delete commented-out code. Link the ADR or issue when a decision is not obvious.
- **ER-CODE-11 No dead code or speculative hooks.** Unused code is deleted; git remembers it.

## 5. Data (ER-DATA)

- **ER-DATA-01 Tenancy columns.** Every business table has `organization_id`, and every commercial table also has `shop_id`, both `NOT NULL` with foreign keys and indexes.
- **ER-DATA-02 Identifiers.** Internal IDs are UUIDs. External IDs are stored as `external_id` with a unique constraint on `(shop_id, external_id)` (plus source when needed). An external ID never replaces an internal one.
- **ER-DATA-03 Time.** `timestamptz` in UTC everywhere. Convert to local time only in the UI.
- **ER-DATA-04 Money.** `NUMERIC(14,4)` in Postgres, `Decimal` in Python, a decimal type on clients; always paired with an ISO 4217 currency code. Floats never hold money. Rounding rules live in one domain module.
- **ER-DATA-05 Migrations are forward-only and compatible.** Expand, migrate data, then contract, across separate releases. Never edit a migration that has been applied anywhere. Every migration ships with tests (pgTAP for RLS and constraints).
- **ER-DATA-06 Row-level security on every exposed table**, plus server-side authorization in the API. RLS is defense in depth, not the only gate.
- **ER-DATA-07 Classify personal data.** Every column holding personal data is marked in the data model doc with its class (buyer contact, address, message content…) and retention period.
- **ER-DATA-08 Source of truth is explicit.** Every stored fact records where it came from (`source`, `observed_at`). Derived figures are recomputable from source records.
- **ER-DATA-09 Audit is append-only.** Audit tables have no update or delete grants for application roles.
- **ER-DATA-10 Migration versions only increase.** A new migration's version is `max(now, newest on main + 1)`, and each pgTAP file takes the next free number. Supabase's `db push` refuses a version older than the newest applied one. CI's `migration-order` job (`tools/migration_order.py`, AC-5.0.1) fails a pull request whose new migration sorts before or with the newest on main, renames or removes a merged migration, or reuses a pgTAP prefix. Before opening a pull request, rebase and renumber.

## 6. Reliability (ER-REL)

- **ER-REL-01 Assume at-least-once.** Every event handler, task and webhook may run twice or out of order. Each one is idempotent through a deterministic idempotency key and a unique constraint.
- **ER-REL-02 External side effects go through an outbox.** The intent is written in the same transaction as the state change, then delivered by a worker with retries. No "write to the DB, then call Etsy, and hope".
- **ER-REL-03 Every network call has a timeout,** bounded retries with exponential backoff and jitter, and only retries what is safe to retry.
- **ER-REL-04 Lifecycle entities use explicit state machines** (orders, tasks, incidents, approvals, quotes). Illegal transitions raise errors and are tested.
- **ER-REL-05 Workers claim work atomically** with a lease and heartbeat, shut down gracefully, and send exhausted work to a dead-letter queue.
- **ER-REL-06 Nothing waits in a loop.** Long waits (supplier quotes, approvals) are persisted and resumed by an event or a scheduled check.
- **ER-REL-07 Degrade honestly.** When a dependency is down, the UI shows stale or unavailable data with its age; it never shows invented values.

## 7. Security (ER-SEC)

- **ER-SEC-01 No secrets in the repository,** clients, logs, chat, agent memory or error messages. gitleaks runs in a pre-commit hook and in CI on every push.
- **ER-SEC-02 Deny by default.** Every endpoint, tool and table starts with no access and grants the minimum.
- **ER-SEC-03 Authorization on the server** from the authenticated context, on every request and every tool call. Client-supplied IDs are lookups, never authority.
- **ER-SEC-04 Validate at every boundary.** Typed schemas for HTTP bodies, query parameters, events, webhooks, tool inputs and model outputs. Reject unknown fields where practical.
- **ER-SEC-05 Untrusted content stays data.** Web pages, supplier data, emails, marketplace messages and model outputs never become instructions, permissions or code.
- **ER-SEC-06 Webhooks** are verified (signature, timestamp tolerance, replay protection) before any processing.
- **ER-SEC-07 Privileged access needs MFA,** and high-risk actions need recent re-authentication.
- **ER-SEC-08 Least-privilege service identities.** API, each worker and the agent runtime have separate credentials with only the rights they need. Clients never receive the Supabase secret or service role key.
- **ER-SEC-09 Supply chain.** Dependencies pinned and scanned; new versions adopted only after a minimum release age (3–7 days); GitHub Actions pinned to full commit SHAs; workflow `permissions` default to none and are granted per job; lockfiles committed; SBOM produced for releases. Security tools are dependencies too and follow the same rules.
- **ER-SEC-10 Security review per change.** The pull request template's security checklist is filled in honestly, citing the OWASP ASVS 5.0 requirement IDs the change touches; anything touching auth, money, PII, webhooks, agents or migrations gets a one-page threat model in the feature's `security.md`.
- **ER-SEC-11 Security baseline.** OWASP ASVS 5.0 Level 2, plus these higher-level requirements pulled forward because of agents and money: 8.3.3 (act with the originating user's permissions), 10.2.3 (minimal OAuth scopes), 2.3.5 (multi-party approval for high-value flows: while the organization has a single owner it is met by step-up re-authentication, and a two-person rule becomes a policy option once a second Admin exists) and chapter 15.4 (safe concurrency). Agent features also follow the OWASP Top 10 for Agentic Applications (2026).

## 8. AI employees, automations and tools (ER-AI)

- **ER-AI-01 Zero-AI first.** If a rule, a query or arithmetic can do it, an LLM does not.
- **ER-AI-02 One gateway.** Every model call goes through the Model Gateway (routing, budget, masking, logging). Importing a provider SDK anywhere else fails the architecture tests.
- **ER-AI-03 Tools are typed capabilities** with input and output schemas, timeouts, retry limits, a side-effect level and an audit record. Drafting and publishing are different tools.
- **ER-AI-04 Side effects outside HTCO need approval** unless a written, versioned policy pre-approves that exact capability, scope and limit.
- **ER-AI-05 No self-approval.** The identity that requests an action can never approve it.
- **ER-AI-06 Budgets are enforced before the call,** not reconciled after it.
- **ER-AI-07 Prompts and policies are code.** Versioned in the repository, reviewed, and recorded with every run.
- **ER-AI-08 Evaluate before promoting.** An employee's autonomy level goes up only after its eval suite passes the agreed thresholds and the owner approves.
- **ER-AI-09 Store outcomes, not hidden reasoning.** We keep inputs, tool calls, outputs, decisions and summaries; we do not store or display chain-of-thought.
- **ER-AI-10 Mask personal data** before it reaches a model unless the task needs it, and record when it did.
- **ER-AI-11 Rule of Two.** One agent run may combine at most two of: reading untrusted content, seeing sensitive data, changing state outside HTCO or communicating externally. Needing all three means splitting the workflow or adding a human approval before the third.
- **ER-AI-12 Tenant and actor come from the run, never from the model.** Tool arguments produced by a model never choose the organization, shop or acting user.
- **ER-AI-13 Four concepts, never mixed** ([ADR-0012](../adr/0012-living-office-organization.md)). Judgment is an employee's job; repeatable actions are automations; capabilities are tools; changing the system is engineering. A deterministic step is never a graph node, and an employee never performs an automation's action directly.
- **ER-AI-14 The office is reviewed data.** Employees, automations, tools and advisors are defined in `registry/` and changed only by pull requests that pass `just registry` and the registry tests. A profile, automation or tool is production-ready only with every specification section defined and a passed evaluation.
- **ER-AI-15 No framework in the business model.** Domain and application code never import LangGraph or any other agent framework; runtime state is never the source of truth for business records (`tests/application/test_framework_independence.py`).
- **ER-AI-16 Every operation leaves a record.** A use case that changes state or calls outside HTCO writes an activity record with actor kind, organization, shop, task, correlation and causation IDs and an outcome.
- **ER-AI-17 Advisors advise.** The Decision Advisor's answers never enter authorization, approval, payment or security decisions; rules run before any advisor call.
- **ER-AI-18 Engineering employees follow the human rules, and more.** Alex, Amir and Iris work only through branches and pull requests, never merge or release, never weaken tests (ER-TEST-02), remove logging, widen their own permissions or bypass branch protection.

## 9. Testing (ER-TEST)

- **ER-TEST-01 Test-first is mandatory** for all production code. See [TESTING_STRATEGY.md](TESTING_STRATEGY.md).
- **ER-TEST-02 Never weaken a test to make it pass.** Changing an assertion requires stating in the pull request why the old expectation was wrong.
- **ER-TEST-03 No real external services in CI.** Etsy, AutoVA, email and model providers are replaced by fakes, recorded fixtures or contract stubs. Live smoke tests run separately and never write.
- **ER-TEST-04 Flaky tests are bugs.** A flaky test is quarantined with an issue the same day and fixed or deleted within a week.
- **ER-TEST-05 Coverage is a floor, not a goal.** Branch coverage of at least 95% for the domain layer and 90% for the application layer; the overall floor rises per phase. Mutation testing runs nightly on money and authorization code from Phase 5.
- **ER-TEST-06 Bugs start with a failing test** that reproduces them; the test stays forever.
- **ER-TEST-07 Test weakening is detected by CI.** New skip or expected-failure markers, deleted tests, lowered coverage or eval thresholds, and changed expected values fail the check unless the pull request has a "Test changes" section explaining why.
- **ER-TEST-08 Security and tenant-isolation tests are never quarantined.** If one fails, the change does not merge.

## 10. Documentation (ER-DOC)

- **ER-DOC-01 Docs change in the same pull request** as the code they describe.
- **ER-DOC-02 Every feature has a folder** `docs/features/<feature>/` with `overview.md`, `design.md`, `tests.md`, `security.md` and `operations.md` (see the [feature template](../features/_template/overview.md)).
- **ER-DOC-03 Non-trivial decisions get an ADR** in `docs/adr/`: new technology, new service, a change of boundary, a trade-off someone will question later.
- **ER-DOC-04 CLAUDE.md and README stay true.** When commands, structure or workflow change, they change in the same pull request.
- **ER-DOC-05 No junk files.** No session notes, scratch files, duplicated docs, empty placeholders or generated reports in the repository. Each document has one owner purpose and is linked from an index.
- **ER-DOC-06 Write for the next reader.** Plain English, short sentences, concrete examples, and links instead of repetition.

## 11. Git, pull requests and releases (ER-GIT)

- **ER-GIT-01 Trunk-based.** `main` is always deployable. Short-lived branches named `feat/…`, `fix/…`, `chore/…`, `docs/…`, `refactor/…`, `test/…`.
- **ER-GIT-02 Conventional Commits** (`feat(orders): ingest Etsy receipts idempotently`). They drive the changelog and version numbers.
- **ER-GIT-03 Small pull requests,** ideally under 400 changed lines excluding generated files, one slice each.
- **ER-GIT-04 Nothing reaches `main` without a pull request,** green CI and a completed checklist. No force pushes to `main`.
- **ER-GIT-05 Squash merge,** with the PR title as the commit message.
- **ER-GIT-06 Releases are tagged** with semantic versions and generated release notes.
- **ER-GIT-07 Commits never contain secrets, personal data, large binaries or generated artifacts** (except checked-in generated clients where an ADR says so).

## 12. Operations (ER-OPS)

- **ER-OPS-01 Every deployable has** `/healthz` (alive) and `/readyz` (dependencies ready) or a heartbeat for workers.
- **ER-OPS-02 Production changes only through CI.** No manual edits in consoles except documented break-glass procedures, which are recorded afterwards.
- **ER-OPS-03 Migrations are gated:** reviewed, tested on a copy, applied by CI with approval, and compatible with the previous release.
- **ER-OPS-04 Risky features ship behind flags** with a tested off switch.
- **ER-OPS-05 Every deploy has a rollback plan.** DigitalOcean rollbacks restore code, not data; data changes need their own plan.
- **ER-OPS-06 Alerts are actionable.** Each alert links to a runbook entry.

## 13. User experience, accessibility and language (ER-UX)

- **ER-UX-01 WCAG 2.2 AA** on the web and equivalent platform guidelines on Windows and Android.
- **ER-UX-02 Design tokens only.** No hard-coded colours, spacing, radii or font sizes in components.
- **ER-UX-03 Every user-facing string is translatable** (Hebrew and English). No string concatenation for sentences.
- **ER-UX-04 RTL is first-class.** Use logical properties (start/end). Wrap tracking numbers, SKUs, IDs and code in LTR isolation.
- **ER-UX-05 Every screen designs its states:** loading, empty, error, stale, offline, no permission.
- **ER-UX-06 Never claim success the backend did not confirm.**
- **ER-UX-07 Every feature reaches the dashboard** (the owner's rule, 2026-10-27). A slice that produces something a user should see or operate (data, a status, a setting or an action) names its web step: a UI slice or a UI acceptance criterion in the same phase or a later named one, web first ([ADR-0016](../adr/0016-web-first.md)). A slice with nothing to show says so in one line: "No UI: <reason>". A plan change that adds a slice with neither does not pass review. [UI_COVERAGE.md](../plan/UI_COVERAGE.md) maps every feature to its web step.

## 14. Performance and cost (ER-PERF)

- **ER-PERF-01 Measure before optimizing,** and record the measurement in the pull request.
- **ER-PERF-02 Every list endpoint is paginated** (cursor-based) and every query used in a hot path has an index and a test that would catch an N+1.
- **ER-PERF-03 Cost impact is part of design.** Pull requests that add model calls, new infrastructure or polling state their expected monthly cost.

## 15. Definition of Ready and Definition of Done

### Ready (before starting a slice)

- [ ] The slice links to a phase plan entry or issue.
- [ ] Acceptance criteria are written (Given/When/Then).
- [ ] Contracts affected are identified (API, events, database).
- [ ] Security and privacy impact is noted (none / low / needs threat note).
- [ ] Dependencies on the owner (accounts, keys, decisions) are resolved.
- [ ] The slice names its web step, or says "No UI: <reason>" (ER-UX-07).

### Done (before merging)

- [ ] Tests were written first and all acceptance criteria are covered.
- [ ] Unit, integration and contract tests pass locally and in CI; no skipped tests without an issue.
- [ ] Format, lint, type check and security scans pass.
- [ ] The feature was verified for real using the `feature-verification` skill, and the evidence is in `docs/features/<feature>/tests.md`.
- [ ] Docs updated: feature folder, API or event contracts, ADR if needed, CLAUDE.md or README if commands changed.
- [ ] Security checklist completed; no secrets or PII in code, logs or fixtures.
- [ ] Observability: logs with correlation IDs, metrics or alerts for new failure modes.
- [ ] Migrations are forward-compatible and tested; rollback plan written for risky changes.
- [ ] The phase plan's slice checkbox is ticked in the same pull request.

## 16. When to stop and ask the owner

Stop and ask before any of these, even if technically possible:

- Applying a migration to production, or deleting or rewriting production data.
- Spending money: new paid services, plan upgrades, raising budgets.
- Writing to Etsy, AutoVA or any external system with real data outside an approved test plan.
- Sending email or messages to customers.
- Changing permissions, roles, security settings or secret material.
- Adding a new external dependency category (a new provider, a new hosted service).
- Anything irreversible, or anything that conflicts with these rules or an ADR.
