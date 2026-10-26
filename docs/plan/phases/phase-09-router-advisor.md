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
- [ ] 9.3 Central router with per-employee policies (routed or pinned, ADR-0023)
- [ ] 9.4 Budget ledger and alerts
- [ ] 9.5 Decision Advisor adapters and decision records
- [ ] 9.6 Decision benchmark
- [ ] 9.7 Eitan activated: fulfillment exception investigation
- [ ] 9.8 Budget and routing settings UI
- [ ] 9.9 Simple vs complex routing, automatic difficulty and browser-agent budgets
- [ ] 9.10 Knowledge base and retrieval (ADR-0024)

**Foundation already in code.** The router and the advisor chain are pure, tested functions: `htco_domain.routing` (`ModelRoutingPolicy`, tiers, data classes), `htco_application.routing.router` (`route`, `RoutingRequest`, `RoutingDecision`, `RoutingRefused`) and `htco_application.decisions.advisor` (`DecisionQuestion`, `advise`, `RulesAdvisor`, `DecisionRecord`). Tests with fakes: `tests/application/test_model_router.py` (Scenario E) and `tests/application/test_decision_advisor.py` (Scenario F). Every employee document in [`registry/employees/`](../../../registry/employees) carries a draft routing policy; [`registry/advisors/jev.toml`](../../../registry/advisors/jev.toml) describes the advisor. The gateway adapters, catalog, budgets, records, benchmark and Eitan's workflow are planned in this phase.

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- **Tool permissions:** the router and the gateway share one actor context. The per-employee routing policy also caps which channels an employee's actions may use: the Browser channel only where the employee's sheet allows it.
- **Simple vs complex routing:**
  - small structured decisions (triage, "which recipe step next", yes/no on a validated page state) go to rules first, then Jev "Ultrafast" where its benchmark passed (ADR-0015);
  - open-ended work goes to the employee's tier;
  - Browser Use's model calls (Phase 8A slice 8A.6) are routed and budgeted like any other employee call.
- **Budgets** include browser-agent model spend per employee and per shop.

## Routing modes, difficulty and knowledge (2026-10-26)

The owner's decisions, recorded in [ADR-0023](../../adr/0023-routing-modes-and-difficulty-assessment.md) and [ADR-0024](../../adr/0024-knowledge-base-and-retrieval.md):

- **A routing mode per employee.**
  - `routed` is ADR-0015's design.
  - `pinned` is one fixed model named by a catalog alias, such as `default-advanced`, never a model ID. It still goes through the gateway, the budget, masking and recording. On ill-health it uses the policy's explicit fallback aliases, or it stops.
  - Only the Owner switches the mode, in 9.8. The change is audited and creates a new profile version (9.2, 9.3, 9.8).
- **Automatic difficulty** for a task that declares none.
  - Rules decide first, then Jev "Ultrafast" (simple, normal or complex) once it passes the 9.6 benchmark on the `task_difficulty` family, and otherwise the employee's default tier.
  - Difficulty only raises the tier, and it is clamped at the employee's maximum tier.
  - The router stays inside the gateway, with no router server. A LiteLLM-style proxy behind the gateway remains a possible future provider adapter (9.3, 9.6, 9.9).
- **A shared knowledge base** (9.10).
  - It runs on pgvector in our Supabase Postgres, with RLS by organization and shop.
  - Embeddings go through the gateway.
  - Search goes structured tools first, then one hybrid full-text and vector query, then the model.
  - Any employee can search through `knowledge.search` under its own knowledge policy. Dana (Phase 14) is the first consumer.

## Goal

Every model call in HTCO goes through one gateway and one deterministic router, under the calling employee's own versioned routing policy, within a budget that is reserved before the call and settled after it, and leaves a routing decision and a usage record. Small structured decisions (triage, routing, urgency) are answered by rules first and, only when rules are inconclusive, by the shared Decision Advisor, after a 200-example benchmark has shown which provider is worth using. The first employee goes to work: **Eitan**, the Fulfillment Inspector, investigates fulfillment exceptions raised by the Phase 5 and 6 automations. Each employee is either routed or pinned to one model by catalog alias, as the Owner chooses. A shared knowledge base lets any employee retrieve the shop's own policies, FAQ, listings and products, under RLS and with no buyer PII, before Dana needs it in Phase 14.

## Why now

Phase 8 proved the runtime with a fake gateway. Real model calls carry cost, privacy and quality risk, so the router, budgets and records must exist before the first employee runs. Eitan comes first because his inputs are already deterministic and well tested (orders, tracking, incidents), his outputs are internal (incidents, tasks), and his work is easy to verify against evidence.

## Scope

**In:** `ModelGateway` adapters for Anthropic and OpenAI using their official SDKs, confined to the gateway adapter package; structured output with schema validation; PII masking; circuit breaker and kill switch per provider; `model_catalog` as data (verified model IDs, API versions, tiers, context sizes, prices with as-of dates, health, evaluation scores) seeded by pull request; the central router (`htco_application.routing.router`) wired into the gateway with each employee's policy from `employee_profile_versions`; `routing_decisions` and `model_usage`; budget ledger with reserve and settle per organization, shop, employee, task, day and month, alerts at 70/90/100% and a safety reserve; `DecisionAdvisor` adapters (TypeSafe Jev through its API; an economy model through the gateway), `decision_advisors` and `decision_records`; the decision benchmark; Eitan's activation; budget and routing settings UI; Scenarios E and F end to end. From ADR-0023: catalog aliases (`model_catalog_aliases`); the `routed` and `pinned` modes with explicit pinned fallbacks; `employee_routing_settings` (the Owner's choice); difficulty assessment by rules, then Jev on the `task_difficulty` family. From ADR-0024: the `vector` extension; `knowledge_sources` and `knowledge_chunks` with RLS; the `KnowledgeBase` port and its pgvector adapter; ingestion jobs, redaction, refresh, deletion and retention; the `knowledge.search` tool with per-employee knowledge policies; the retrieval eval set.

**Out:** the Decision & Model Observatory ([Phase 15](phase-15-office-executive.md)); routing experiments and model A/B tests ([Phase 21](phase-21-growth-scale.md)); optimization proposals on routing ([Phase 18](phase-18-optimization.md)); remediation of the incidents Eitan investigates (allowlisted code or a person until Amir in [Phase 19](phase-19-engineering.md)).

## Owner prerequisites

- Anthropic and OpenAI API keys created for HTCO, in separate projects or workspaces with spend limits at the provider too.
- TypeSafe early-access account and API key for Jev, and TypeSafe's terms and data-handling documentation (to verify the facts in [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), which come from secondary sources).
- Monthly AI budget cap (D-06).
- Review of the labelled benchmark questions (at least 200 per task family; drafted from Phase 5–7 records and fixtures, corrected by the owner).
- Approval of Eitan's autonomy level (proposed: L1, acting only inside HTCO).
- Review of the labelled `task_difficulty` set: at least 200 tasks labelled simple, normal or complex, drafted from Phase 5–8 records and fixtures, with no buyer PII.
- The routing mode of each employee: `routed` by default, `pinned` where he wants one fixed model, with the alias and its fallback.
- The shop's FAQ, and review of the retrieval eval set: at least 200 labelled queries in Hebrew and English.

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

`model_catalog` rows: provider, model ID, API version, tier (economy, balanced, advanced, critical, vision, image), capabilities, context size, input and output prices with an as-of date and a source link, health, evaluation scores. Seeded and changed only by pull request after checking the provider's documentation; no model ID appears in code or in a routing policy (policies name tiers, or catalog aliases when pinned).

**Aliases (ADR-0023).**
- `model_catalog_aliases` gives one stable name to exactly one catalog row at a time, with an as-of date and a source. Examples: `default-advanced`, `default-economy`, `default-embedding` (ADR-0024).
- A pinned policy and the embedding step name an alias. Upgrading or replacing the model behind it is one catalog pull request.
- Aliases are lower-case words joined by hyphens (`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, at most 64 characters). An alias's tier is its row's tier.

| ID | Acceptance criterion |
|---|---|
| AC-9.2.1 | A test fails if a model ID string appears anywhere outside the catalog seed. Aliases are not model IDs, and a policy or tool that names a model ID instead of an alias fails the same test. |
| AC-9.2.2 | A catalog row without a price as-of date or a source link is rejected. |
| AC-9.2.3 | A model marked unhealthy is skipped by the router until it is healthy again, and the skip is visible in the routing decision. |
| AC-9.2.4 | Every alias resolves to exactly one catalog row. The seed check refuses each of these: an alias without a row, an alias with two rows, a retired row behind an alias, or an alias that does not match the pattern. |
| AC-9.2.5 | Repointing an alias is a catalog seed change that records its as-of date and source. Routing decisions made before and after the change name the alias and the model each one actually used. |
| AC-9.2.6 | The registry check refuses an employee policy whose `pinned_model` or `pinned_fallback` names an unknown alias, or an alias whose tier is above the policy's `max_tier`. |

### 9.3 — Central router with per-employee policies

The gateway calls `route` for every request with the employee's routing policy (from the session's profile version), the task's difficulty, data class and required capabilities. The router returns `NO_AI` for deterministic tasks, refuses data above the policy's class, raises the tier to the task's difficulty within the policy's maximum, picks the cheapest healthy catalog model that fits providers, capabilities, data class and budget, and falls back through the policy's fallback tiers, never below the task's difficulty. Each decision is one `routing_decisions` row (employee, policy version, tier, model, reason, trace ID) and each call one `model_usage` row (tokens, cache tokens, cost). Automations that have a named AI step use the same router (FR-AUT-04).

**Routing modes (ADR-0023).**
- **`routed`** (the default) is the behaviour above.
- **`pinned`** resolves the policy's `pinned_model` alias through the catalog. The model does not change with difficulty.
  - **Limits still apply.** The policy's providers, capabilities, data class and per-task budget still hold, and so does the rule that the alias's tier is not above `max_tier`. A call that breaks one is refused, not re-routed.
  - **Ill-health.** The model counts as unavailable when it is unhealthy, its circuit is open, its kill switch is on, or its row is retired. The router then tries the `pinned_fallback` aliases in order. If none is available, it refuses with `pinned-unavailable` and records the refusal, and the task follows its own failure path. Pinned mode never falls back to routed selection.

**Difficulty.**
- A task that declares no difficulty is assessed once, by `assess_difficulty`, before `route`: rules first, then the advisor's `task_difficulty` family if 9.6 enabled it and the employee's `decision_advisor` allows it, otherwise the policy's `default_tier`.
- The labels map to tiers: simple → economy, normal → balanced, complex → advanced.
- An assessed difficulty only raises the tier from `default_tier`, and it is clamped at `max_tier`. A declared minimum above `max_tier` is refused, as today.
- Each decision records the mode, the alias, the resolved model, the difficulty and its source, and two flags: `difficulty_clamped` and `difficulty_above_pinned`.

| ID | Acceptance criterion |
|---|---|
| AC-9.3.1 | **Scenario E** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)), end to end through the gateway: two employees with different policies (Eitan's and a second draft policy with a lower data class and a smaller budget) send the same task shape; each gets the route its own policy allows, the second is refused confidential data, and both decisions are recorded with their policy versions. The pure test in `tests/application/test_model_router.py` keeps passing. |
| AC-9.3.2 | Every model call writes exactly one routing decision and one usage row; a call without a routing decision is impossible (the gateway refuses it). |
| AC-9.3.3 | A deterministic task routed to `NO_AI` makes no provider call and records the `NO_AI` decision. |
| AC-9.3.4 | Changing an employee's routing policy creates a new profile version through the registry sync; decisions keep the version they used. |
| AC-9.3.5 | **Scenario E, extended:** a pinned employee and a routed employee send the same task shape. The pinned one uses its alias's model whatever the task's difficulty and whatever the catalog's prices. The routed one gets the cheapest fitting model. Both decisions record their mode, their policy version and the model used. |
| AC-9.3.6 | A pinned call goes through the same gateway path as a routed one. Its budget is reserved before the call and settled after it. Buyer PII is masked under the AC-9.1.3 rules. It writes one routing decision and one usage row. A fake provider that records what it receives proves the masking. |
| AC-9.3.7 | A pinned call that would break the policy is refused before any provider request, and the refusal is recorded. That covers data above `max_data_class`, a provider not in `allowed_providers`, a missing capability, and a reservation over the per-task budget. No other model is tried in its place. |
| AC-9.3.8 | When the pinned model is unhealthy, its circuit is open, its kill switch is on or its row is retired, the router tries the `pinned_fallback` aliases in order, each under the same limits. With no fallback left, it refuses with `pinned-unavailable`, records the refusal, and makes no provider call. It never falls back to a routed choice. |
| AC-9.3.9 | A task without a declared difficulty is assessed once per task. Rules that decide make no advisor call. The advisor is asked only when the rules are inconclusive, the `task_difficulty` family is enabled (AC-9.6.5) and the employee's policy allows it. A timeout, an invalid answer or a failure falls back to `default_tier` without delaying the task. Each assessment records its source: `rules`, `advisor` or `default`. |
| AC-9.3.10 | Property test over every policy in `registry/employees/` and every label: an assessed difficulty never produces a tier above `max_tier` (it is clamped, and `difficulty_clamped` is recorded) and never below `default_tier`. A declared minimum above `max_tier` is still refused. |
| AC-9.3.11 | A difficulty question sent to the advisor carries no buyer name, contact, address or message text. It uses structured task features and a summary that has been PII-masked and capped in length, and fixtures with fake PII prove it. Its cost is reserved and settled against the employee's and the task's budget. |

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

Before any production use of the advisor (FR-MDL-08): for each task family used in this phase (fulfillment exception triage and incident urgency at least), a labelled set of at least 200 questions; the harness of Phase 8 compares **rules-only**, **Jev** and **an economy model** on: accuracy per group (shop, exception type), false-approve rate, schema validity, p95 latency, cost per decision, and failure handling (timeouts, invalid answers, outages). The result is a report and a decision recorded in ADR-0015's validation log: which provider order, if any, is enabled per family. Families for later employees are benchmarked in the phase that activates them. The **`task_difficulty`** family (ADR-0023) is benchmarked here too, with at least 200 tasks labelled simple, normal or complex. Its safety measure, in place of false approvals, is the **under-rating rate**: a complex task judged simple gets too weak a model, while over-rating only costs money.

| ID | Acceptance criterion |
|---|---|
| AC-9.6.1 | **Scenario F** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) with real providers: the report shows, per family and group, the six measures for rules-only, Jev and the economy model on the same labelled set, with dataset version and run date. |
| AC-9.6.2 | A provider is enabled for a family only if it beats rules-only on accuracy without exceeding the agreed false-approve rate, keeps schema validity at or above 99%, and stays within the cost and p95 latency limits agreed at kickoff; otherwise the family stays rules-only. |
| AC-9.6.3 | Injected outages and invalid answers during the run are handled by fallback, and none produces an unvalidated value. |
| AC-9.6.4 | Benchmark datasets contain no buyer PII and are versioned; the run is repeatable from the stored dataset version. |
| AC-9.6.5 | The `task_difficulty` report compares rules-only, Jev "Ultrafast" and an economy model on the same labelled set. It shows accuracy per label, the under-rating rate, schema validity, p95 latency and cost per assessment. Jev is enabled for difficulty only if it beats rules-only on accuracy and keeps the under-rating rate at or below the limit agreed at kickoff. Otherwise assessment stays rules, then `default_tier`. The decision is recorded in ADR-0023's validation log. |

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
| AC-9.7.6 | For every cancelled or refunded order whose cause is not given by Etsy (Etsy's API carries no cancellation reason, and a refund's reason is often empty), Eitan writes a suggested reason from the evidence: the refund note, order and shipment timeline, tracking, and, from Phase 14, Dana's linked buyer conversation. The suggestion is stored as an internal annotation marked "AI-suggested" with its evidence and confidence, shown on the order and in the refunds-by-reason statistics, and editable by the owner; Etsy's own reason, when present, always wins. Nothing is written to Etsy or sent to the buyer. |

### 9.8 — Budget and routing settings UI

Owner and Admin screens:
- budgets and thresholds per level;
- current spend and reservations;
- provider kill switches;
- each employee's routing policy. It is read-only, because changes go through `registry/`. The exception is the **routing mode**, below.
- the benchmark report.

**Routing mode (ADR-0023).** For each employee, the **Owner** switches between `routed` and `pinned` and, for `pinned`, picks the alias and its ordered fallback list from the catalog aliases the policy allows (within `max_tier`, the providers and the data class). He can also reset the employee to "follow the registry".
- The change is stored in `employee_routing_settings`. It writes a new `employee_profile_versions` row (origin `owner_setting`) and one audit entry.
- Admins see the mode, read-only.
- A later registry sync keeps the Owner's setting in every new profile version until he resets it.

| ID | Acceptance criterion |
|---|---|
| AC-9.8.1 | Budget changes require an `aal2` session with step-up, write an audit record and take effect for the next reservation. |
| AC-9.8.2 | Spend figures state their as-of time and come from `model_usage`, never from estimates shown as actuals. |
| AC-9.8.3 | Only the Owner can change an employee's routing mode, alias or fallback. The change needs an `aal2` session with step-up and an `Idempotency-Key`. Negative tests: an Admin or another member is refused (403), and so is a session without step-up. No employee, agent or automation has a tool that changes any routing setting. The tool registry contains none, and the policy engine denies one. |
| AC-9.8.4 | Each change writes exactly one new `employee_profile_versions` row (origin `owner_setting`) and one audit entry: actor, employee, from → to and the new version, with no personal data. Routing decisions after the change name the new version, and decisions made before it keep the old one. |
| AC-9.8.5 | The screen offers only aliases the policy allows. Choosing any other alias, through the API, is refused. A registry sync after an Owner change keeps the Owner's mode in the new version until he resets it to "follow the registry", and both paths are tested. |

### 9.9 — Simple vs complex routing, automatic difficulty and browser-agent budgets

Small structured decisions go to rules first, then to Jev where benchmarked. Open-ended work goes to the employee's tier, and that tier now follows the automatic difficulty of ADR-0023 when the task declares none: simple tasks stay on cheap tiers, and complex ones are raised up to the employee's cap. Browser Use calls (8A.6) follow the employee's mode: routed by difficulty, or pinned to its alias.

| ID | Acceptance criterion |
|---|---|
| AC-9.9.1 | A fixture set of 200 labelled "simple" browser decisions is answered by rules or Jev only after the benchmark passes; otherwise by the economy tier. |
| AC-9.9.2 | A Browser Use session that would exceed the employee's per-task budget is stopped before the call, and the task is handed to a person. |
| AC-9.9.3 | On a fixture mix of labelled simple, normal and complex tasks that declare no difficulty, routed employees use the tiers their assessments allow, within each cap. The run reports the cost against routing everything at `default_tier` and at `max_tier`, and no task gets a tier above its employee's `max_tier`. |
| AC-9.9.4 | A pinned employee's Browser Use calls use its alias, under the same per-task budget and the same stop-before-the-call rule as AC-9.9.2. With the pin and its fallbacks unavailable, the session stops and hands over to a person; it is never re-routed. |

### 9.10 — Knowledge base and retrieval

A shared knowledge base that any employee can search ([ADR-0024](../../adr/0024-knowledge-base-and-retrieval.md)), ready before Dana (Phase 14).

**Store.** The `vector` extension, and two tables, `knowledge_sources` and `knowledge_chunks`, which hold redacted text, a `tsvector` and a `vector(n)` embedding with its alias. They have RLS by organization and shop, composite foreign keys, an HNSW index and a GIN index. No client role writes to them.

**What is embedded.** Only an allowlist of source kinds: `shop_policy`, `faq`, `listing` (its public text, not price, stock or state), and `product` (not supplier, cost or margin): content that the owner or the shop wrote. **No reply is ingested in v1** (ADR-0024 §2: replies wait for their own ADR). Raw buyer messages (DC2) and buyer contact data (DC1) are never embedded.

**Retrieval.**
1. Structured tools come first, for order, listing state, tracking, prices and stock.
2. Then `knowledge.search`: one SQL statement with the organization, shop and allowed-kind filter in its `WHERE` clause, full-text plus vector similarity fused by reciprocal rank, `k` at most 10.
3. The model comes last, and cites chunk IDs.

**Embeddings.** They go through the Model Gateway via the catalog's `default-embedding` alias: routed, budgeted, masked and recorded. Ingestion runs under the system routing policy `knowledge-ingest` (`max_data_class = internal`), and every query is masked before it is embedded.

**The `KnowledgeBase` port.** It has `ingest`, `search` and `delete`, a pgvector adapter in persistence, and an in-memory adapter for tests behind the same contract.

**Access and records.** Each employee's sheet sets a knowledge policy: the kinds it may search, its maximum `k` and its languages. Dana's is the first. The search scope comes from the task's session, never from the model. Each search is recorded with a keyed hash (HMAC) of the query, never its text.

**Ingestion.** These are idempotent `knowledge.ingest` jobs, keyed by source and content hash. A listing change, a policy change or an FAQ edit triggers one, and a nightly reconciliation catches drift. On deletion, a source's chunks go in the same transaction.

**Retention.** The shop's knowledge is purged with the shop under ADR-0018's 30-day rule. No chunk derives from a buyer conversation, so no DC2 retention or erasure rule applies.

| ID | Acceptance criterion |
|---|---|
| AC-9.10.1 | pgTAP: a member reads chunks only from their organizations and from shops they may access, and the worker only from its job's organization. Organization-wide chunks (no shop) are readable by every member of that organization, shop-scoped roles included, and by no other organization. Reads across organizations and across shops return nothing. No client role can insert, update or delete. The worker's policy is organization-wide, so the shop boundary for an employee's search is proved by AC-9.10.5. |
| AC-9.10.2 | Ingestion accepts only the four allowed kinds. A source carrying a buyer message, an email body (DC2), any reply text (a draft, an approved or sent reply, a mailbox copy), or a buyer name, contact or address field (DC1) is refused before chunking, and the refusal is recorded. Listing price, stock and state are never indexed. |
| AC-9.10.3 | Every accepted source passes redaction before chunking. A fixture corpus of owner-written sources with fake names, emails, phones, postal addresses, tracking numbers and order numbers leaves none of them in a stored chunk, or in any request the fake embedding provider receives. A fixture reply, in every form (a draft, an approved and sent reply with quoted history, a mailbox copy), is refused and recorded, and none of its words reaches a stored chunk or the fake provider. A chunk in which the residual check still finds PII is quarantined, not embedded, and reported. A fixture search query with a fake email and phone reaches the fake embedding provider masked. |
| AC-9.10.4 | Every embedding call, whether at ingestion or for a query, goes through the Model Gateway with the catalog's embedding alias. Ingestion runs under the system routing policy `knowledge-ingest`, pinned to that alias with `max_data_class = internal`; a test shows the gateway refuses a chunk above `internal` under it. Each call writes one routing decision and one usage row, and it is reserved against the right budget (the organization and shop for ingestion, the employee and task for search). An architecture test finds no embedding SDK outside the gateway adapter, and AC-9.2.1 finds no model ID in code. |
| AC-9.10.5 | `knowledge.search` runs as one SQL statement that applies the organization, shop and allowed-kind filter before ranking, so no other tenant's row is scored. It fuses full-text and vector ranks by reciprocal rank fusion, returns at most `k` hits (`k` at most 10) above the minimum score, and each hit carries its kind, source, version, chunk ID and score. Vectors from another embedding model are never compared. The organization and shop come only from the task's session: the tool's input schema rejects an organization or shop the model supplies, and a worker search for one shop never returns another shop's chunk. The `tsquery` is built with `websearch_to_tsquery` or `plainto_tsquery` (never `to_tsquery` on raw text), and a query over the length cap is refused before it is parsed or embedded. |
| AC-9.10.6 | In an employee graph fixture, a question about an order's state, tracking or price is answered from the structured tool, not from the knowledge base. Those facts are not indexed, and a search for them returns nothing. |
| AC-9.10.7 | A listing or policy change re-indexes only that source, idempotently: replaying the same event or content hash makes no embedding call. A changed source replaces its chunks in one transaction. A failed ingest is dead-lettered and can be retried. The nightly reconciliation finds and repairs a planted drift. |
| AC-9.10.8 | Deleting or deactivating a source deletes its chunks in the same transaction, and no orphan chunk remains. A shop disconnect purges the shop's knowledge under ADR-0018. No chunk carries a link to a buyer conversation (schema test). Each rule has its own test. |
| AC-9.10.9 | An employee without the `knowledge.search` grant is denied. An employee's allowed kinds and maximum `k` are enforced, and asking for more returns only what the policy allows. Each search writes an activity record with the employee, the task, a keyed query hash (an HMAC with a server-side key, so the same query under another key gives another hash), the result IDs and the latency, and never the query text. |
| AC-9.10.10 | Retrieved chunks reach the model only in a delimited, quoted reference block with their provenance, never as instructions. An injection corpus is ingested into a test shop, for example listing text that orders a refund or an FAQ that asks for a tool call. With it in place, the evaluation shows no tool call, no change to grants, routing or policy, and no promise the reply policy forbids. Instruction-like content is flagged in the source's record. |
| AC-9.10.11 | A versioned retrieval eval set runs in the Phase 8 harness: at least 200 labelled queries in Hebrew and English across the kinds, with no buyer PII. It reports recall@5, MRR and p95 latency for hybrid, full-text only and vector only. Cross-tenant and cross-shop leakage, measured with two seeded organizations and shops, must be zero. The other thresholds are agreed at kickoff. Dana's activation (14.7) requires a passing run. |
| AC-9.10.12 | One contract suite for the `KnowledgeBase` port passes for both the pgvector adapter and the in-memory one. Swapping the adapter needs no change outside the composition root. |

## Test plan

Fake providers for unit tests; recorded provider responses for adapter contract tests; property tests for the budget ledger; router tests over every employee policy in `registry/`; the benchmark as a repeatable harness run; Eitan's evaluation set nightly; one live smoke test per provider with a tiny budget; Scenarios E and F end to end. In addition:
- property tests that an assessed difficulty stays between `default_tier` and `max_tier`;
- pinned-mode tests: limits, fallback order, the `pinned-unavailable` stop;
- negative tests for the Owner-only mode change;
- pgTAP for the knowledge tables, across organizations and shops;
- the redaction, ingestion, deletion and retention tests;
- the injection corpus on a test shop;
- the retrieval eval set as a repeatable harness run;
- the `KnowledgeBase` contract suite against both adapters.

## Security checklist

- [ ] Provider keys only in the gateway's environment; no key in prompts, logs, checkpoints or decision records.
- [ ] PII masking proven by fixtures; data class limits per employee enforced by the router.
- [ ] TypeSafe's data handling and terms checked before any real question is sent; no buyer PII in advisor questions.
- [ ] The advisor has no path to authorization decisions (tested).
- [ ] Rule of Two documented for Eitan's graph.
- [ ] Pinned calls keep every control of a routed call: gateway, budget, masking, recording and the policy's limits. An unavailable pin never becomes a different model without the policy naming it (tested).
- [ ] Only the Owner changes a routing mode, with `aal2` and step-up, audited. No actor has a tool for it (negative tests).
- [ ] Difficulty questions carry no buyer PII, and an assessed difficulty can never raise an employee above its `max_tier`.
- [ ] Knowledge tables have RLS by organization and shop (pgTAP), and no raw DC1 or DC2 data reaches them: no raw buyer message and no reply text is ever embedded in v1. Only owner-written policies, FAQs and listing or product text are.
- [ ] A search's organization and shop come only from the task's session; a model-supplied scope is rejected (tested). Ingestion runs under the `knowledge-ingest` system policy (`max_data_class = internal`), and queries are masked before they are embedded.
- [ ] Retrieved text is data, never instructions (injection corpus). Search records hold a keyed query hash (HMAC), never the text.

## Documentation

`docs/features/model-gateway/`, `docs/features/decision-advisor/`, `docs/features/knowledge-base/`, `docs/features/employees/eitan.md`; ADR-0004, ADR-0015, ADR-0023 (the `task_difficulty` benchmark decision) and ADR-0024 (the retrieval eval result) validation logs; COST_MODEL updated with real per-call, per-inspected-order and embedding costs; DATA_MODEL tables marked created.

## Demo

Show Eitan investigating a real or synthetic late order: evidence gathered, triage recorded (rules or advisor), incident updated, correction task created, recovery verified when tracking appears; show the routing decision and cost of each call; trigger a budget cap; show the benchmark report and which families use the advisor. Then:
- the Owner pins an employee to an alias, and the next decision shows the pin and the new profile version;
- the pin is made unhealthy, and the call follows its fallback or stops;
- a task without a declared difficulty is assessed and clamped at its employee's cap;
- a `knowledge.search` over the shop's policies and FAQ returns only that shop's chunks, with the retrieval eval report.

## Exit gate

- [ ] All AC-9.x pass, including Scenarios E and F end to end.
- [ ] Benchmark decision recorded per task family; the advisor is enabled only where it passed.
- [ ] Eitan is `ready`, his autonomy approved by the owner, and his routing policy measured.
- [ ] Budget caps and provider kill switches verified in production.
- [ ] Every employee's routing mode confirmed by the Owner (routed unless he pinned it), and the `task_difficulty` decision recorded.
- [ ] The knowledge base is live for the pilot shop: cross-tenant leakage of zero, and the retrieval eval passing, ready for Dana in Phase 14.

## Rollback

Eitan's kill switch; provider kill switches; disable the advisor per family (rules-only); router policy versions can be reverted through the registry sync. In addition:
- The Owner sets a pinned employee back to `routed`, or to "follow the registry".
- Disabling the `task_difficulty` family leaves assessment to rules, then `default_tier`.
- Revoking an employee's `knowledge.search` grant, or switching off ingestion, stops retrieval. Purging a shop's knowledge rows is safe, because the sources can be ingested again.

## Risks

| Risk | Mitigation |
|---|---|
| Jev facts differ from the secondary sources | Verify against TypeSafe's documentation at kickoff; the economy model and rules-only are always available |
| Cost surprises | Reserve before call; low caps during the pilot; per-task caps per employee |
| Too few real exceptions for a meaningful evaluation set | Synthetic exceptions from fixtures, labelled by the owner; production review in the first weeks |
| A pinned model is deprecated or degraded | Repoint the alias in one catalog pull request; explicit fallbacks; a pin without one stops instead of drifting to another model |
| Jev under-rates difficulty | The under-rating limit in AC-9.6.5; every assessment recorded; a declared minimum always wins; the cap is never exceeded |
| PII reaches the knowledge base | The source allowlist, deterministic redaction plus gateway masking, quarantine on a residual hit, and fixtures (AC-9.10.2, AC-9.10.3) |
| Retrieved text tries to steer an employee | Retrieved text goes in a quoted reference block with provenance, no buyer-written sources are ingested, and the injection corpus is tested (AC-9.10.10) |
| The HNSW index outgrows the database plan | Size it from the corpus at kickoff; the store can be swapped behind the `KnowledgeBase` port under a new ADR |

## Cost impact

AI usage within the owner's cap; planning figure for Eitan from the evaluation run (recorded at the gate). TypeSafe usage priced per ADR-0015 if enabled. Embedding cost for ingestion and queries, measured during the retrieval eval and recorded at the gate. pgvector storage stays inside the existing database plan.
