# Phase 8 — Model router, Decision Advisor and tools

| | |
|---|---|
| **Milestone** | **M3 — Models and tools** |
| **Repositories** | `htco-platform` (most work), `htco-clients` (router console, Decision Advisor page, AI tools registry, AI cost and quota dashboard) |
| **Size** | L |
| **Depends on** | [Phase 7](phase-07-pilot-office.md) (pilot route, budgets, real pilot records to route, benchmark and measure) |
| **Requirements** | FR-ADM-03, FR-AGT-03, FR-CAT-05 (provenance), FR-FIN-03 (AI unit costs), FR-MDL-01, FR-MDL-02, FR-MDL-03, FR-MDL-04, FR-MDL-05, FR-MDL-06, FR-MDL-07, FR-MDL-08, FR-MDL-09, FR-OFC-07 (AI Tools Center), FR-OFC-08, FR-TOOL-01, FR-TOOL-02, FR-TOOL-03 |
| **Status** | PLANNED |

## Progress

- [ ] 8.1 Model Gateway and provider adapters
- [ ] 8.2 Verified model catalog as data
- [ ] 8.3 Central router with versioned per-employee routing policies
- [ ] 8.4 Budgets, cost ledger and alerts
- [ ] 8.5 Decision Advisor adapters and decision records
- [ ] 8.6 Decision benchmark and safe disable
- [ ] 8.7 Image generation adapter and asset provenance
- [ ] 8.8 Tool registry in the database and the MCP gateway
- [ ] 8.9 Router console, Decision Advisor page and AI tools registry
- [ ] 8.10 AI cost and quota dashboard

**Foundation already in code.** The router and the advisor chain are pure, tested functions: `htco_domain.routing` (`ModelRoutingPolicy`, tiers, data classes), `htco_application.routing.router` (`route`, `RoutingRequest`, `RoutingDecision`, `RoutingRefused`) and `htco_application.decisions.advisor` (`DecisionQuestion`, `advise`, `RulesAdvisor`, `DecisionRecord`). Tests with fakes: `tests/application/test_model_router.py` (Scenario E) and `tests/application/test_decision_advisor.py` (Scenario F). Every employee document in [`registry/employees/`](../../../registry/employees) carries a routing policy; [`registry/advisors/jev.toml`](../../../registry/advisors/jev.toml) describes the advisor; [`registry/tools/`](../../../registry/tools) lists the tools. From [Phase 6](phase-06-workforce-claude-runtime.md): `tool_definitions` rows, the tool bridge and the per-session `htco` MCP server; from [Phase 7](phase-07-pilot-office.md): a one-row model catalog, the pilot routing decisions, `budgets`, `budget_ledger`, `model_usage` and four active employees. Everything below that is not listed here is planned in this phase.

## Goal

Every model use in HTCO is chosen by one deterministic router, under the calling employee's own **versioned and approved** routing policy, from a **verified catalog** (no model ID or price in code), within a budget reserved before the call and settled after it, and leaves a routing decision and a cost record. The router chooses the **runtime and the model** for an employee step (the Claude runtime runs Anthropic models only) and the provider for non-session calls (`models.complete`, the advisor's economy model, images). Small structured decisions are answered by rules first and, only when a 200-example benchmark has shown it is worth it, by the Decision Advisor (Jev or an economy model), with a safe disable. Image generation exists as a port and an OpenAI adapter with provenance, ready for Nika in [Phase 12](phase-12-product-studio.md). Tools are managed as data in the database and exposed to sessions only through the MCP gateway. The owner sees routing, decisions, tools, AI cost and quota on honest screens.

No employee is activated in this phase. Eitan's activation moved to [Phase 10](phase-10-fulfillment-incidents.md).

## Why now

The pilot ran on one model with simple caps. Before more employees, providers and money-adjacent work arrive (Phases 9–13), model choice must become policy (versioned, evaluated, approved), cost must be attributable per unit of work, the advisor must be benchmarked before anyone relies on it, and tools must be governed as versioned data. The pilot gives real records to benchmark and measure against.

## Scope

**In:**

- `ModelGateway` port and adapters for Anthropic and OpenAI with their official SDKs, confined to `htco_model_router`; structured output validation; PII masking; circuit breaker and kill switch per provider.
- How the Claude runtime fits the gateway rule: `claude-agent-sdk` stays confined to `htco_agent_runtime_claude`; the session's model, `fallback_model`, budget and billing mode come only from the router and the ledger, and the session's `model_usage` is recorded through the same ledger as gateway calls.
- `model_catalog` as data with verified IDs, API versions, tiers, capabilities, context sizes, prices with as-of dates and source links, health and evaluation scores.
- The central router with the master plan's inputs and outputs, per-employee routing policies versioned through the registry with an approval and an evaluation (master plan test 12).
- Budgets and the cost ledger at every level, alerts at 70%, 90% and 100%, a safety reserve, and `cost_per_*` metrics.
- `DecisionAdvisor` adapters: TypeSafe Jev through its API (the Jev `DecisionAdapter`) and an economy model through the gateway; `decision_advisors`, `decision_records`; the benchmark of at least 200 labelled examples per family; safe disable (master plan test 7).
- `ImageGenerationPort`, `OpenAIImageAdapter`, `asset_provenance`, the media tools `media.generate_gpt` and `media.edit` with recorded fixtures ([ADR-0018](../../adr/0018-image-generation-specialist.md); master plan test 8 at adapter level).
- The tool registry in the database exposed through the MCP gateway: tool versions, schemas, health, quotas, cost per call, readiness.
- Web screens: Model Router Console, Decision Advisor (Jev) page, AI Tools Registry, AI Cost and Quota dashboard (clients specification screens 29–32).
- Scenarios E and F end to end.

**Out:**

- Eitan and any other activation ([Phase 10](phase-10-fulfillment-incidents.md) onward).
- Nika, the Creative Studio and real image use ([Phase 12](phase-12-product-studio.md)).
- Routing experiments and model A/B tests ([Phase 21](phase-21-growth-scale.md)); optimization proposals on routing ([Phase 14](phase-14-governance.md)).
- The full Decision and Model Observatory inside the complete office ([Phase 16](phase-16-web-office.md)); this phase builds its first screens.
- Any `APPROVED_SUBSCRIPTION_RUNTIME` use ([ADR-0017](../../adr/0017-auth-and-billing-policy.md): off, needs a new ADR).

## Owner prerequisites

- An OpenAI API key for HTCO in its own project with a provider-side spend limit (images and text candidates); the Anthropic key from Phase 0 with a provider-side spend limit too.
- A TypeSafe early-access account and API key for Jev, and TypeSafe's terms and data-handling documentation, to verify the facts in [ADR-0015](../../adr/0015-decision-advisor-and-routing.md) that come from secondary sources.
- The monthly AI budget cap (D-06) for after the pilot, and the safety reserve.
- Review of the labelled benchmark questions (at least 200 per family, drafted from Phase 4–7 records and fixtures, corrected by the owner).
- Approval of the routing policy versions proposed for the four pilot employees (each with its evaluation result).
- Whether HTCO's Anthropic or OpenAI organizations hold prepaid API credits, and where the owner reads their balance, so the dashboard can show credits separately from spend.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. For slices 8.9 and 8.10 also load `design-system` in `htco-clients`.
2. Read this file, then [PLAN.md](../PLAN.md) (§5 mandatory tests 7, 8 and 12; §6 gates), [ADR-0004](../../adr/0004-model-gateway.md), [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), [ADR-0014](../../adr/0014-tool-registry.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ADR-0018](../../adr/0018-image-generation-specialist.md), [SOFTWARE_DESIGN](../../architecture/SOFTWARE_DESIGN.md) (model gateway section), [THREAT_MODEL](../../architecture/THREAT_MODEL.md) and [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios) (Scenarios E and F).
3. At kickoff, verify every catalog entry (model IDs, API versions, prices, limits) against the providers' current documentation, and verify Jev's API, schema support, pricing and data handling against TypeSafe's documentation. Record each fact with an as-of date and a source link. If a fact cannot be verified, the entry stays disabled; never guess.
4. Work in slice order: 8.1 → 8.2 → 8.3 → 8.4 (the router replaces the Phase 7 pilot route only when 8.1–8.4 pass), then 8.5 → 8.6, then 8.7, then 8.8, then 8.9 and 8.10. One pull request per slice; tests first.
5. Use recorded fixtures for every provider contract test (Anthropic, OpenAI text, OpenAI images, Jev): success, 400, 401, 429, 5xx, timeout, content-policy refusal, invalid schema. Live calls are limited to one smoke test per provider with a tiny budget, run manually and recorded.
6. Never in this phase: hard-code a model ID, price or limit; import a provider SDK outside `htco_model_router` (or `claude-agent-sdk` outside `htco_agent_runtime_claude`); let the advisor's answer reach the policy engine or `decide_tool_access`; send buyer PII to Jev or an image model; enable the advisor for a family that failed its benchmark; grant `media.generate_gpt` to any real employee (Nika arrives in Phase 12); show per-token cost for subscription sessions; activate an employee.
7. Stop and ask the owner when: a provider fact contradicts the plan or an ADR; a benchmark result is borderline against the agreed thresholds; a routing policy change would raise an employee's maximum tier, data class or per-task cap; TypeSafe's terms do not allow the intended data; costs approach the cap.
8. At the end, run the demo against the deployed development environment, update the docs below, record cost actuals and ask the owner for the M3 sign-off.

## Slices

### 8.1 — Model Gateway and provider adapters

`ModelGateway` port in `htco_application` and adapters for Anthropic and OpenAI using the official Python SDKs, confined to `htco_model_router`. Structured outputs are validated against JSON Schemas from `packages/contracts`. A PII masking step runs before any prompt leaves the gateway, driven by the task's data class and the employee policy's maximum data class. Circuit breaker, timeouts, retries and kill switch per provider. Provider keys live only in the environment of the component that makes the call (the gateway for API calls, the Claude runtime adapter for sessions, per [ADR-0017](../../adr/0017-auth-and-billing-policy.md)).

For employee steps the Claude Agent SDK is the transport: the runtime adapter asks the router for the route, takes `model` and `fallback_model` from it, reserves budget through the ledger, and writes `model_usage` from `ResultMessage.model_usage` priced from the catalog. One records path serves sessions and gateway calls.

| ID | Acceptance criterion |
|---|---|
| AC-8.1.1 | import-linter fails if any package other than `htco_model_router` imports `anthropic` or `openai`, or any package other than `htco_agent_runtime_claude` imports `claude_agent_sdk` (FR-MDL-01). |
| AC-8.1.2 | Structured output that fails schema validation is an error, is never passed on, and is recorded. |
| AC-8.1.3 | Buyer names, emails and addresses are masked before a prompt leaves the gateway unless the task's data class and the employee's policy both allow them; fixtures prove it, and every unmasked call is recorded. |
| AC-8.1.4 | When a provider fails repeatedly its circuit opens and calls follow the policy's fallback chain; with its kill switch on, no call or session reaches it, and the UI shows it disabled with the reason. |
| AC-8.1.5 | One live smoke test per provider with a tiny budget passes in the deployed environment (manual, recorded with date and cost). |

### 8.2 — Verified model catalog as data

`model_catalog` rows: provider, model ID, API version, tier (economy, balanced, advanced, critical, vision, image), capabilities, context size, input and output prices (and per-image prices for image models) with an as-of date, a source link, `verified_at` and `verified_by`, health, data-class limits and evaluation scores. Seeded and changed only by pull request after checking the provider's documentation. Routing policies name tiers and providers, never model IDs. The one-row pilot catalog of Phase 7 becomes the first row of this catalog.

| ID | Acceptance criterion |
|---|---|
| AC-8.2.1 | A test fails if a model ID string appears anywhere outside the catalog seed (code, prompts, policies, tests other than the catalog's own). |
| AC-8.2.2 | A catalog row without a price as-of date, a source link or `verified_at` is rejected; a row older than the re-verification interval agreed at kickoff is flagged on the router console. |
| AC-8.2.3 | A model marked unhealthy is skipped by the router until it is healthy again, and the skip is visible in the routing decision. |

### 8.3 — Central router with versioned per-employee routing policies

The router takes the master plan's inputs (employee, organization and shop, task capability, risk, data class, required modalities and tools, estimated context tokens, cost and latency budget, permitted providers, quota state, policy version, evaluation scorecard) and returns: runtime, provider, model reference, billing mode, decision reason code, fallback chain, whether approval is required, the budget reserved and the policy version. Decision order: (1) no AI when a deterministic path exists (`NO_AI`), (2) security gate (data class, provider permitted), (3) capability fit, (4) quality threshold from the employee's evaluation scorecard, (5) cheapest healthy model that fits cost and latency, (6) fallback on error, never below the task's difficulty, (7) the result's quality check. The Claude runtime is offered only Anthropic models; a policy that needs another provider for the main reasoning names another runtime, and a profile whose runtime is not enabled waits with an incident (ADR-0016).

**Policy changes are versioned, evaluated and approved** (master plan test 12): a routing policy change is a new employee profile version through the registry sync; the sync accepts it only with an `evaluation_runs` row comparing the old and new policy on the employee's evaluation set, and an `approvals` row from the owner. Running sessions keep the version they started with. Every decision is one `routing_decisions` row; every call or step one `model_usage` row.

| ID | Acceptance criterion |
|---|---|
| AC-8.3.1 | **Scenario E** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) end to end: two pilot employees with different policies send the same task shape; each gets the route its own policy allows, the one with the lower data class is refused confidential data, and both decisions are recorded with their policy versions. The pure test in `tests/application/test_model_router.py` keeps passing. |
| AC-8.3.2 | **Master plan test 12**: a routing policy change without an evaluation run, or without the owner's approval, is rejected by the sync and nothing is written; with both, exactly one new profile version is created and decisions after it reference it. |
| AC-8.3.3 | Every model call and every employee step writes exactly one routing decision and one usage row; a call or session without a routing decision is refused. |
| AC-8.3.4 | A deterministic task routed to `NO_AI` makes no provider call and records the decision. |
| AC-8.3.5 | Quota state is an input: a provider at its rate limit is skipped or the task waits per policy; nothing fails silently and automations keep running (master plan test 3 re-run with the full router). |
| AC-8.3.6 | The Phase 7 pilot route is retired: the four pilot employees run on the full router with their approved policy versions, and their evaluation sets still pass. |

### 8.4 — Budgets, cost ledger and alerts

Reserve the estimated cost before a call or step and settle the actual cost after, at every level: organization, shop, employee, task, day and month. Alerts at **70%, 90% and 100%** through notifications, once per level and period. New work stops at the cap; a safety reserve is kept for tasks marked safety-critical. `AuthAndBillingPolicy` is connected to the ledger: every entry carries the billing mode, and API credits (prepaid balance) are recorded as their own entries, separate from spend.

Unit cost metrics, computed by deterministic jobs from `model_usage`, `budget_ledger` and task and correlation IDs: `cost_per_research_task`, `cost_per_listing`, `cost_per_order`, `cost_per_ticket`, `cost_per_image`, `cost_per_incident`, plus per employee and per department. A metric whose unit does not exist yet (for example tickets before Phase 15) shows "no data", never zero or an estimate.

| ID | Acceptance criterion |
|---|---|
| AC-8.4.1 | A call or step whose reservation would exceed any cap is refused before any provider request, and the refusal is recorded; the task waits with the reason. |
| AC-8.4.2 | Settlement replaces the reservation with the actual cost; a failed call releases its reservation (property tests over interleavings). |
| AC-8.4.3 | Crossing 70%, 90% and 100% sends one notification each per level and period, not one per call. |
| AC-8.4.4 | At the monthly cap only safety-critical tasks can use the reserve, and that use is reported. |
| AC-8.4.5 | Every `cost_per_*` metric states its formula, source tables and as-of time, and reconciles with `model_usage` for the same period; units with no records show "no data". |

### 8.5 — Decision Advisor adapters and decision records

Adapters behind the `DecisionAdvisor` protocol of `htco_application.decisions.advisor`: the **Jev `DecisionAdapter`** (TypeSafe's API, in `htco_model_router`), and an economy model through the gateway (routed, budgeted and recorded like any call). `decision_advisors` rows come from `registry/advisors/`; every attempt is a `decision_records` row (question family, provider, outcome `answered`, `inconclusive`, `invalid` or `failed`, value, latency, cost) and an activity record with the `advisor` actor kind. An employee may ask only as its policy allows (`never`, `when_rules_inconclusive`, `preferred`), through the `decisions.advise` tool. Providers stay disabled per family until 8.6 passes for that family. **Safe disable**: one switch per family and one per provider turn the advisor off; callers then get `inconclusive` and follow their rules.

| ID | Acceptance criterion |
|---|---|
| AC-8.5.1 | A question that rules answer reaches no provider; an inconclusive one tries providers in order; an answer outside the question's schema is rejected and the next provider is tried; every attempt is recorded. |
| AC-8.5.2 | **Master plan test 7**: when Jev times out, errors, or returns an invalid schema (recorded fixtures), the rules or the fallback provider answer, or the question returns `inconclusive` and the caller escalates; no unvalidated value is used and no check is bypassed. |
| AC-8.5.3 | An employee whose policy says `never` cannot reach the advisor (the tool bridge denies `decisions.advise`). |
| AC-8.5.4 | No advisor answer reaches the policy engine or `decide_tool_access`: an answer "approve" to a question about a payment or a permission changes nothing (Scenario F, authorization part). |
| AC-8.5.5 | Safe disable of a family or a provider takes effect for the next question, is audited, and is shown on the Decision Advisor page. |

### 8.6 — Decision benchmark and safe disable

Before any production use of the advisor (FR-MDL-08): for each family used now, a labelled set of **at least 200 questions** with no buyer PII. Families for this phase come from the pilot: **task routing** (which department or employee should take a request, for Maya) and **finding severity triage** (for Noa). Families of later employees (fulfillment exception triage, incident urgency, policy screening, ticket classification) are benchmarked in the phase that activates them. The Phase 6 harness compares **rules-only**, **Jev** and **an economy model** on accuracy per group, false-approve rate, schema validity, p95 latency, cost per decision and failure handling. The result is a report and a decision recorded in ADR-0015's validation log: which provider order, if any, is enabled per family.

| ID | Acceptance criterion |
|---|---|
| AC-8.6.1 | **Scenario F** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) with real providers: the report shows, per family and group, the six measures for rules-only, Jev and the economy model on the same labelled set, with dataset version and run date. |
| AC-8.6.2 | A provider is enabled for a family only if it beats rules-only on accuracy without exceeding the agreed false-approve rate, keeps schema validity at or above 99%, and stays within the cost and p95 latency limits agreed at kickoff; otherwise the family stays rules-only. |
| AC-8.6.3 | Injected outages and invalid answers during the run are handled by fallback, and none produces an unvalidated value. |
| AC-8.6.4 | Benchmark datasets are versioned, contain no buyer PII (CI check) and the run is repeatable from the stored dataset version. |

### 8.7 — Image generation adapter and asset provenance

Per [ADR-0018](../../adr/0018-image-generation-specialist.md): `ImageGenerationPort` in `htco_application` (operations `generate` and `edit`; typed request with brief, source asset references, size, count, data class and budget) and `OpenAIImageAdapter` in `htco_model_router`, the only place the OpenAI SDK is used for images. Model ID, price and limits come from the catalog's `image` tier. The tools `media.generate_gpt` and `media.edit` (renaming the draft `media.generate_image` and `media.edit_image` in `registry/tools/creative_media.toml`) reserve budget, call the port, store the result in Supabase Storage, write an `asset_provenance` row (prompt brief, input asset IDs and hashes, provider, model reference, parameters, cost, created by, task, licence check result) and return asset references, never bytes, to the session. Every generated asset starts as `NEEDS_QA`. In this phase only a fixture employee shaped like Nika holds these tools; Nika is activated in [Phase 12](phase-12-product-studio.md).

| ID | Acceptance criterion |
|---|---|
| AC-8.7.1 | **Master plan test 8, adapter level**: a generated asset (recorded fixture) has a complete `asset_provenance` row, a recorded price from the catalog, storage permissions limited to its organization and shop, and state `NEEDS_QA`; the publish guard (a domain function used by Phase 12's automation) refuses it until it is approved. |
| AC-8.7.2 | Contract tests with recorded fixtures cover success, 400, 429, 5xx, content-policy refusal and timeout; each failure is typed, recorded and releases or settles its reservation. |
| AC-8.7.3 | A request containing buyer personal data, or an input asset from another shop, is refused before any provider call. |
| AC-8.7.4 | Only profiles granted `media.generate_gpt` or `media.edit` can call them (grants matrix); the OpenAI key is never in a session's `env`. |

### 8.8 — Tool registry in the database and the MCP gateway

Tools are capabilities, not employees ([ADR-0014](../../adr/0014-tool-registry.md)). `tool_definitions` (from Phase 6) become the single source for what a session can see: each version has owner, family, typed input and output schemas, required capabilities, shop scope, permission policy, side-effect class, idempotency rule, audit requirement, quotas, timeout, cost per call, health and readiness. The **MCP gateway** is the component that builds each session's in-process `htco` MCP server from these rows and the profile's grants: only `ready`, healthy, granted tool versions appear, with their schemas. The MCP layer grants no authority by itself: every call still goes through the tool bridge, `decide_tool_access` and the policy engine. Tool health checks and per-tool quotas run as deterministic jobs. New tools enter through `registry/tools/` and the reviewed sync, never through the API.

| ID | Acceptance criterion |
|---|---|
| AC-8.8.1 | A session's MCP server lists exactly the granted, `ready`, healthy tool versions of its profile version; a tool marked unhealthy or `draft` disappears from new sessions without a deploy. |
| AC-8.8.2 | Every tool has contract tests from its schemas; the sync refuses a tool version without schemas, owner, side-effect class or audit requirement. |
| AC-8.8.3 | A tool over its quota fails with a typed error recorded in `tool_invocations`, and the step follows the task's retry rules. |
| AC-8.8.4 | **Scenario H** through the MCP gateway: a call to a tool that is not in the session's server, or named with another shop, is denied and recorded, even when the model insists. |
| AC-8.8.5 | `GET /v1/tools`, `GET /v1/tools/{name}/versions` and `GET /v1/tools/{name}/usage` (counts, failures, cost) are in the OpenAPI contract and pass Schemathesis; there is no write route. |

### 8.9 — Router console, Decision Advisor page and AI tools registry

Web screens in `htco-clients/apps/web` (clients specification screens 29–31), for Owner and Admin:

- **Model Router Console**: the catalog (provider, verified ID, capabilities, price with as-of date, benchmark, latency, data class, health); each employee's routing policy (permitted providers, policy version, quality threshold, per-task budget, fallback chain, risk and approval requirements, provider status, deployment state), with its version history, evaluation result and approval; routing traces that explain the choice, the fallback and the actual cost, with no model reasoning. Policies are read-only here: changes go through `registry/` with evaluation and approval.
- **Decision Advisor (Jev) page**: question schemas per family, provider availability, gold dataset accuracy from 8.6, decision history, overrides, and the safe-disable switches (step-up).
- **AI Tools Registry**: tools with family, version, risk, readiness, health, grants by employee, usage and cost.

| ID | Acceptance criterion |
|---|---|
| AC-8.9.1 | Every figure comes from `model_catalog`, `routing_decisions`, `model_usage`, `decision_records`, `evaluation_runs` or `tool_invocations`, states its as-of time and links to its records. |
| AC-8.9.2 | Provider kill switches and advisor safe disable require an `aal2` session with step-up, write an audit record, and take effect for the next call. |
| AC-8.9.3 | Jev and GPT images never appear as employees; neither is shown with policy authority (clients specification §13). |

### 8.10 — AI cost and quota dashboard

Clients specification screen 32: API usage and spend per provider, employee, department, shop, task type and day; reservations; caps and alert thresholds; quota and rate-limit events (429s, waits, fallbacks used); the `cost_per_*` metrics of 8.4; budget settings (step-up). **Honest economics** ([ADR-0017](../../adr/0017-auth-and-billing-policy.md) rule 6): actual API cost comes from `model_usage` and the catalog; API credits are shown as a separate balance, never mixed with spend; subscription usage (development only) is shown, if at all, as "not measured by HTCO", and HTCO never invents per-token cost or savings for subscription sessions. The status of each billing mode (`API_PRODUCTION` on, `SUBSCRIPTION_INTERACTIVE` development only, `APPROVED_SUBSCRIPTION_RUNTIME` off) is shown with its reason.

| ID | Acceptance criterion |
|---|---|
| AC-8.10.1 | Spend figures state their as-of time and come from `model_usage` and `budget_ledger`, never from estimates shown as actuals; a test compares the dashboard's totals with database sums for the same period. |
| AC-8.10.2 | API credits and paid spend are separate figures; no screen or API computes a per-token cost for a subscription session. |
| AC-8.10.3 | Budget changes require an `aal2` session with step-up, write an audit record and take effect for the next reservation. |
| AC-8.10.4 | The dashboard works in Hebrew and English, light and dark, at phone width, and passes the Phase 3 accessibility checks. |

## Test plan

Fake providers for unit tests; recorded fixtures for every provider contract (Anthropic, OpenAI text and images, Jev); property tests for the budget ledger; router tests over every employee policy in `registry/`; the registry sync tests for policy changes (test 12); the benchmark as a repeatable harness run; the pilot employees' evaluation sets re-run on the full router; MCP gateway tests (tool listing, health, quotas, Scenario H); dashboard and console contract and browser tests; one live smoke test per provider with a tiny budget. Mandatory tests owned here: **7**, **8** (adapter level), **12**; re-run here: **3** with the full router.

## Security checklist

- [ ] Provider keys only in the environment of the component that uses them; no key in prompts, logs, tool results, decision records or screens.
- [ ] PII masking proven by fixtures; data-class limits per employee enforced by the router.
- [ ] TypeSafe's data handling and terms checked before any real question is sent; no buyer PII in advisor questions or image prompts.
- [ ] The advisor has no path to authorization decisions (tested).
- [ ] The MCP gateway exposes only granted, ready tools; the bridge remains the authority.
- [ ] Generated assets stored with shop-scoped permissions; provenance complete.
- [ ] Threat model updated for the new providers, the advisor and image generation.

## Documentation

`docs/features/model-gateway/`, `docs/features/model-router/`, `docs/features/decision-advisor/`, `docs/features/image-generation/`, `docs/features/tool-registry/`, `docs/features/ai-cost/`; ADR-0004, ADR-0015 (benchmark decision), ADR-0017 and ADR-0018 validation logs; [COST_MODEL.md](../../cost/COST_MODEL.md) with real per-call, per-step and per-unit costs; DATA_MODEL tables marked created; [TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md) mandatory-test status; [ASSUMPTIONS.md](../../product/ASSUMPTIONS.md) with the verified provider and Jev facts.

## Demo

Show the catalog with verified entries and as-of dates; change a pilot employee's routing policy in `registry/` and show the sync refusing it until the evaluation and the owner's approval exist; run Maya and Noa on the same task shape and show their different routes and costs; show the benchmark report and which families use the advisor; inject a Jev timeout and an invalid answer and show the fallback; disable the advisor safely; generate an image with a recorded fixture and show its provenance and `NEEDS_QA`; mark a tool unhealthy and show it gone from a new session; trigger the 70% and 100% alerts; walk the AI cost and quota dashboard with credits shown separately.

## Exit gate

- [ ] All AC-8.x pass, including Scenarios E and F end to end and master plan tests 7, 8 (adapter level) and 12.
- [ ] Benchmark decision recorded per family; the advisor is enabled only where it passed.
- [ ] The four pilot employees run on the full router with approved policy versions and passing evaluations.
- [ ] Budget caps, alerts, provider kill switches and advisor safe disable verified on the deployed environment.
- [ ] The owner signs off **M3 — Models and tools**.

## Rollback

Provider kill switches; advisor safe disable per family and provider (rules-only); routing policy versions reverted through the registry sync; the Phase 7 one-row route can be restored by flag until the gate passes; image tools stay ungranted to real employees; screens behind feature flags. All new tables are additive.

## Risks

| Risk | Mitigation |
|---|---|
| Jev facts differ from secondary sources, or its terms block our data | Verify at kickoff; rules-only and the economy model are always available; safe disable |
| Provider IDs or prices change | Catalog as data with as-of dates and re-verification; no ID in code |
| Cost surprises from new providers | Reserve before call; provider-side spend limits; alerts at 70/90/100% |
| Too few real examples for a 200-question set | Synthetic questions from fixtures, labelled by the owner; families stay rules-only until the set exists |
| Router change degrades a pilot employee | Policy changes require an evaluation run and approval; sessions keep their version; revert through the sync |

## Cost impact

AI usage within the owner's monthly cap; benchmark runs (planning figure recorded at the gate); OpenAI image fixtures cost nothing, the live smoke test a few cents (price verified at kickoff); TypeSafe usage priced per ADR-0015 if enabled, verified at kickoff.
