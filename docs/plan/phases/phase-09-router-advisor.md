# Phase 9 — Model router and Decision Advisor

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (budget and routing settings) |
| **Size** | M |
| **Depends on** | [Phase 8](phase-08-workforce-runtime.md) |
| **Requirements** | FR-ADM-03, FR-AGT-09, FR-INC-01, FR-MDL-01, FR-MDL-02, FR-MDL-03, FR-MDL-04, FR-MDL-05, FR-MDL-06, FR-MDL-07, FR-MDL-08, FR-MDL-09, FR-ORD-09 |
| **Status** | Not started |

## Progress

- [ ] 9.1 Model Gateway and provider adapters
- [ ] 9.2 Verified model catalog as data
- [ ] 9.3 Central router with per-employee policies
- [ ] 9.4 Budget ledger and alerts
- [ ] 9.5 Decision Advisor adapters and decision records
- [ ] 9.6 Decision benchmark
- [ ] 9.7 Eitan activated: fulfillment exception investigation
- [ ] 9.8 Budget and routing settings UI

**Foundation already in code.** The router and the advisor chain are pure, tested functions: `htco_domain.routing` (`ModelRoutingPolicy`, tiers, data classes), `htco_application.routing.router` (`route`, `RoutingRequest`, `RoutingDecision`, `RoutingRefused`) and `htco_application.decisions.advisor` (`DecisionQuestion`, `advise`, `RulesAdvisor`, `DecisionRecord`). Tests with fakes: `tests/application/test_model_router.py` (Scenario E) and `tests/application/test_decision_advisor.py` (Scenario F). Every employee document in [`registry/employees/`](../../../registry/employees) carries a draft routing policy; [`registry/advisors/jev.toml`](../../../registry/advisors/jev.toml) describes the advisor. The gateway adapters, catalog, budgets, records, benchmark and Eitan's workflow are planned in this phase.

## Goal

Every model call in HTCO goes through one gateway and one deterministic router, under the calling employee's own versioned routing policy, within a budget that is reserved before the call and settled after it, and leaves a routing decision and a usage record. Small structured decisions (triage, routing, urgency) are answered by rules first and, only when rules are inconclusive, by the shared Decision Advisor, after a 200-example benchmark has shown which provider is worth using. The first employee goes to work: **Eitan**, the Fulfillment Inspector, investigates fulfillment exceptions raised by the Phase 5 and 6 automations.

## Why now

Phase 8 proved the runtime with a fake gateway. Real model calls carry cost, privacy and quality risk, so the router, budgets and records must exist before the first employee runs. Eitan comes first because his inputs are already deterministic and well tested (orders, tracking, incidents), his outputs are internal (incidents, tasks), and his work is easy to verify against evidence.

## Scope

**In:** `ModelGateway` adapters for Anthropic and OpenAI using their official SDKs, confined to the gateway adapter package; structured output with schema validation; PII masking; circuit breaker and kill switch per provider; `model_catalog` as data (verified model IDs, API versions, tiers, context sizes, prices with as-of dates, health, evaluation scores) seeded by pull request; the central router (`htco_application.routing.router`) wired into the gateway with each employee's policy from `employee_profile_versions`; `routing_decisions` and `model_usage`; budget ledger with reserve and settle per organization, shop, employee, task, day and month, alerts at 70/90/100% and a safety reserve; `DecisionAdvisor` adapters (TypeSafe Jev through its API; an economy model through the gateway), `decision_advisors` and `decision_records`; the decision benchmark; Eitan's activation; budget and routing settings UI; Scenarios E and F end to end.

**Out:** the Decision & Model Observatory ([Phase 15](phase-15-office-executive.md)); routing experiments and model A/B tests ([Phase 21](phase-21-growth-scale.md)); optimization proposals on routing ([Phase 18](phase-18-optimization.md)); remediation of the incidents Eitan investigates (allowlisted code or a person until Amir in [Phase 19](phase-19-engineering.md)).

## Owner prerequisites

- Anthropic and OpenAI API keys created for HTCO, in separate projects or workspaces with spend limits at the provider too.
- TypeSafe early-access account and API key for Jev, and TypeSafe's terms and data-handling documentation (to verify the facts in [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), which come from secondary sources).
- Monthly AI budget cap (D-06).
- Review of the labelled benchmark questions (at least 200 per task family; drafted from Phase 5–7 records and fixtures, corrected by the owner).
- Approval of Eitan's autonomy level (proposed: L1, acting only inside HTCO).

## Slices

### 9.1 — Model Gateway and provider adapters

`ModelGateway` port and adapters for Anthropic and OpenAI using the official Python SDKs; structured outputs validated against JSON Schemas from `packages/contracts`; a PII masking step before any prompt leaves the gateway, driven by the task's data class and the employee policy's maximum data class; circuit breaker and kill switch per provider; timeouts and retries per provider. Provider keys live only in the gateway's environment.

| ID | Acceptance criterion |
|---|---|
| AC-9.1.1 | An architecture test fails if any package other than the gateway adapter imports a provider SDK (FR-MDL-01). |
| AC-9.1.2 | Structured output that fails schema validation is treated as an error and never passed on; the attempt is recorded. |
| AC-9.1.3 | Buyer names, emails and addresses are masked before a prompt leaves the gateway unless the task's data class and the employee's policy both allow them; fixtures prove it, and every unmasked call is recorded. |
| AC-9.1.4 | When a provider fails repeatedly, its circuit opens and calls go to the policy's fallback; when its kill switch is on, no call reaches it. |
| AC-9.1.5 | One live smoke test per provider with a tiny budget passes in the deployed environment (manual, recorded). |

### 9.2 — Verified model catalog as data

`model_catalog` rows: provider, model ID, API version, tier (economy, balanced, advanced, critical, vision, image), capabilities, context size, input and output prices with an as-of date and a source link, health, evaluation scores. Seeded and changed only by pull request after checking the provider's documentation; no model ID appears in code or in a routing policy (policies name tiers).

| ID | Acceptance criterion |
|---|---|
| AC-9.2.1 | A test fails if a model ID string appears anywhere outside the catalog seed. |
| AC-9.2.2 | A catalog row without a price as-of date or a source link is rejected. |
| AC-9.2.3 | A model marked unhealthy is skipped by the router until it is healthy again, and the skip is visible in the routing decision. |

### 9.3 — Central router with per-employee policies

The gateway calls `route` for every request with the employee's routing policy (from the session's profile version), the task's difficulty, data class and required capabilities. The router returns `NO_AI` for deterministic tasks, refuses data above the policy's class, raises the tier to the task's difficulty within the policy's maximum, picks the cheapest healthy catalog model that fits providers, capabilities, data class and budget, and falls back through the policy's fallback tiers, never below the task's difficulty. Each decision is one `routing_decisions` row (employee, policy version, tier, model, reason, trace ID) and each call one `model_usage` row (tokens, cache tokens, cost). Automations that have a named AI step use the same router (FR-AUT-04).

| ID | Acceptance criterion |
|---|---|
| AC-9.3.1 | **Scenario E** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)), end to end through the gateway: two employees with different policies (Eitan's and a second draft policy with a lower data class and a smaller budget) send the same task shape; each gets the route its own policy allows, the second is refused confidential data, and both decisions are recorded with their policy versions. The pure test in `tests/application/test_model_router.py` keeps passing. |
| AC-9.3.2 | Every model call writes exactly one routing decision and one usage row; a call without a routing decision is impossible (the gateway refuses it). |
| AC-9.3.3 | A deterministic task routed to `NO_AI` makes no provider call and records the `NO_AI` decision. |
| AC-9.3.4 | Changing an employee's routing policy creates a new profile version through the registry sync; decisions keep the version they used. |

### 9.4 — Budget ledger and alerts

Reserve the estimated cost before a call and settle the actual cost after, at every level (organization, shop, employee, task, day, month); alerts at 70%, 90% and 100% through notifications; new work stops at the cap, with a reserved budget kept for safety-critical tasks; the per-task cap of each employee's policy applies.

| ID | Acceptance criterion |
|---|---|
| AC-9.4.1 | A call whose reservation would exceed any cap is refused before any provider request, and the refusal is recorded. |
| AC-9.4.2 | Settlement replaces the reservation with the actual cost; a failed call releases its reservation (property tests over interleavings). |
| AC-9.4.3 | Crossing 70%, 90% and 100% sends one notification each per period, not one per call. |
| AC-9.4.4 | At the monthly cap, only tasks marked safety-critical can use the reserve, and that use is reported. |

### 9.5 — Decision Advisor adapters and decision records

Adapters behind the `DecisionAdvisor` protocol of `htco_application.decisions.advisor`: TypeSafe Jev through its API, and an economy model through the gateway (so it is routed, budgeted and recorded like any call). `decision_advisors` rows from `registry/advisors/`; every attempt is a `decision_records` row (question family, provider, outcome `answered`, `inconclusive`, `invalid` or `failed`, value, latency, cost) and an activity record with the `advisor` actor kind. An employee may ask only as its policy allows (`never`, `when_rules_inconclusive`, `preferred`), through the `decisions.advise` tool. Providers stay disabled in production until 9.6 passes.

| ID | Acceptance criterion |
|---|---|
| AC-9.5.1 | A question that rules answer reaches no provider; an inconclusive one tries providers in order; an answer outside the question's schema is rejected and the next provider is tried; every attempt is recorded. |
| AC-9.5.2 | An employee whose policy says `never` cannot reach the advisor (the tool bridge denies `decisions.advise`). |
| AC-9.5.3 | No answer of the advisor reaches the policy engine or `decide_tool_access`: an answer "approve" for a question about a payment or a permission changes nothing (Scenario F, authorization part). |
| AC-9.5.4 | When every provider fails, the question returns `inconclusive` with the failure recorded, and the caller follows its rule-based fallback (escalate to a person). |

### 9.6 — Decision benchmark

Before any production use of the advisor (FR-MDL-08): for each task family used in this phase (fulfillment exception triage and incident urgency at least), a labelled set of at least 200 questions; the harness of Phase 8 compares **rules-only**, **Jev** and **an economy model** on: accuracy per group (shop, exception type), false-approve rate, schema validity, p95 latency, cost per decision, and failure handling (timeouts, invalid answers, outages). The result is a report and a decision recorded in ADR-0015's validation log: which provider order, if any, is enabled per family. Families for later employees are benchmarked in the phase that activates them.

| ID | Acceptance criterion |
|---|---|
| AC-9.6.1 | **Scenario F** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) with real providers: the report shows, per family and group, the six measures for rules-only, Jev and the economy model on the same labelled set, with dataset version and run date. |
| AC-9.6.2 | A provider is enabled for a family only if it beats rules-only on accuracy without exceeding the agreed false-approve rate, keeps schema validity at or above 99%, and stays within the cost and p95 latency limits agreed at kickoff; otherwise the family stays rules-only. |
| AC-9.6.3 | Injected outages and invalid answers during the run are handled by fallback, and none produces an unvalidated value. |
| AC-9.6.4 | Benchmark datasets contain no buyer PII and are versioned; the run is repeatable from the stored dataset version. |

### 9.7 — Eitan activated: fulfillment exception investigation

Eitan ([`registry/employees/eitan.toml`](../../../registry/employees/eitan.toml)) investigates the exceptions that the automations raise: drifts from Order Deduplication & Verification and reconciliation, fulfillment incidents from Incident Detection & Escalation, and discrepancies between Etsy, AutoVA (when data is available) and the CRM. For each, he gathers evidence through read tools, triages (rules first, the advisor if his policy and 9.6 allow), opens or updates incidents, creates correction tasks for a person or requests an allowlisted automation run through `automation.trigger`, and verifies recovery against evidence. Routine processing stays with the automations. Activation follows the rule of [PLAN §5](../PLAN.md#5-gates-that-apply-to-every-phase) gate 7:

1. All 22 sections of Eitan's specification sheet are completed from `draft` to `defined` in a reviewed pull request (inputs and outputs as JSON Schemas, triggers, skills, business knowledge, memory, budgets, risk and approvals, evaluation, failure handling, performance metrics).
2. His evaluation set (real-shape exceptions from Phases 5–6 with expected investigation outcomes) is built with the Phase 8 harness and passes the thresholds in his `evaluation` section.
3. The owner approves his autonomy level (recorded as an approval).
4. His routing policy is measured on the evaluation set and the first production weeks: cost per inspected order, tiers used, refusals, advisor use.

| ID | Acceptance criterion |
|---|---|
| AC-9.7.1 | Eitan's sheet has no `draft` section; the registry sync accepts `ready` only with a recorded passing evaluation run. |
| AC-9.7.2 | On the evaluation set, Eitan's findings cite evidence for every conclusion, and he never marks an incident resolved: incidents resolve only on evidence through the Phase 6 rules. |
| AC-9.7.3 | Eitan cannot write shipments, contact buyers or trigger an automation outside his `allowed_automations` (denials recorded). |
| AC-9.7.4 | Repeated failure on a task escalates to a person with evidence (Maya from Phase 15; Amir for technical causes from Phase 19). |
| AC-9.7.5 | Cost per inspected order and routing outcomes are reported per week, and stay within his policy's per-task cap. |

### 9.8 — Budget and routing settings UI

Owner and Admin screens: budgets and thresholds per level, current spend and reservations, provider kill switches, each employee's routing policy (read-only: changes go through `registry/`), and the benchmark report.

| ID | Acceptance criterion |
|---|---|
| AC-9.8.1 | Budget changes require an `aal2` session with step-up, write an audit record and take effect for the next reservation. |
| AC-9.8.2 | Spend figures state their as-of time and come from `model_usage`, never from estimates shown as actuals. |

## Test plan

Fake providers for unit tests; recorded provider responses for adapter contract tests; property tests for the budget ledger; router tests over every employee policy in `registry/`; the benchmark as a repeatable harness run; Eitan's evaluation set nightly; one live smoke test per provider with a tiny budget; Scenarios E and F end to end.

## Security checklist

- [ ] Provider keys only in the gateway's environment; no key in prompts, logs, checkpoints or decision records.
- [ ] PII masking proven by fixtures; data class limits per employee enforced by the router.
- [ ] TypeSafe's data handling and terms checked before any real question is sent; no buyer PII in advisor questions.
- [ ] The advisor has no path to authorization decisions (tested).
- [ ] Rule of Two documented for Eitan's graph.

## Documentation

`docs/features/model-gateway/`, `docs/features/decision-advisor/`, `docs/features/employees/eitan.md`; ADR-0004 and ADR-0015 validation logs (benchmark result); COST_MODEL updated with real per-call and per-inspected-order costs; DATA_MODEL tables marked created.

## Demo

Show Eitan investigating a real or synthetic late order: evidence gathered, triage recorded (rules or advisor), incident updated, correction task created, recovery verified when tracking appears; show the routing decision and cost of each call; trigger a budget cap; show the benchmark report and which families use the advisor.

## Exit gate

- [ ] All AC-9.x pass, including Scenarios E and F end to end.
- [ ] Benchmark decision recorded per task family; the advisor is enabled only where it passed.
- [ ] Eitan is `ready`, his autonomy approved by the owner, and his routing policy measured.
- [ ] Budget caps and provider kill switches verified in production.

## Rollback

Eitan's kill switch; provider kill switches; disable the advisor per family (rules-only); router policy versions can be reverted through the registry sync.

## Risks

| Risk | Mitigation |
|---|---|
| Jev facts differ from the secondary sources | Verify against TypeSafe's documentation at kickoff; the economy model and rules-only are always available |
| Cost surprises | Reserve before call; low caps during the pilot; per-task caps per employee |
| Too few real exceptions for a meaningful evaluation set | Synthetic exceptions from fixtures, labelled by the owner; production review in the first weeks |

## Cost impact

AI usage within the owner's cap; planning figure for Eitan from the evaluation run (recorded at the gate). TypeSafe usage priced per ADR-0015 if enabled.
