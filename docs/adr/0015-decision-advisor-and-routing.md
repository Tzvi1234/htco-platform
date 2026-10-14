# ADR-0015: A shared Decision Advisor and one central router with a policy per employee

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Amends:** [ADR-0004](0004-model-gateway.md) (routing policy becomes per employee)
- **Related:** [ADR-0012](0012-living-office-organization.md), FR-MDL-02, FR-MDL-08

## Context

Many office decisions are small and structured: which department takes a task, how urgent an incident is, whether a candidate product needs review, whether a task needs a stronger model. Large language models answer them well but expensively; deterministic rules answer many of them for free. TypeSafe's **Jev** is a decision-only model that returns typed answers (choice or score) instead of text.

What we know about Jev (2026-10-14, secondary sources, to be verified against TypeSafe's documentation and a benchmark before use): launched in September 2026; reachable through TypeSafe's early-access API and OpenRouter; priced around USD 0.042 per million input tokens with output not billed; context budget reported as 32K or 64K tokens depending on the source; no public weights or license; accuracy claims are the vendor's own.

Each employee also needs its own model choices: a customer-service reply on personal data, an engineering change and a product research summary have different quality, privacy, latency and cost needs.

## Decision drivers

- Rules first: no inference call when rules decide (Zero-AI first).
- Provider independence: Jev must be replaceable by an economy model or a later service.
- Advisors advise; they never authorize.
- One router for consistency and observability; one versioned policy per employee for fit.

## Options considered

1. **Jev for every decision.** Cheap per call, but an unbenchmarked single provider in the critical path.
2. **A provider-independent `DecisionAdvisor` port: rules, then a chain of providers, with schema validation and records.**
3. **No advisor; employees decide everything with their own models.** Simple, more expensive, harder to measure.

## Decision

Option 2, plus per-employee routing policies on the central router.

**Decision Advisor** (`htco_application.decisions`):

- A `DecisionQuestion` is typed: `choice` (with options) or `score` (0 to 1), with structured context.
- `advise` runs deterministic rules first; only an inconclusive question reaches providers, tried in order (today: `typesafe-jev`, then an economy model through the gateway). An answer outside the question's schema is rejected; a failing provider moves to the next; every attempt is a `DecisionRecord` (provider, outcome, value, detail) for the decision log and benchmarks.
- An employee's routing policy says when it may ask: `never`, `when_rules_inconclusive` or `preferred`.
- The advisor never authorizes: tool access, approvals, payments and security policies are decided by the policy engine alone.
- Jev appears in the office as a shared specialist (`registry/advisors/jev.toml`): identity and activity history, no department, no headcount.
- Before production use, Jev is compared with rules-only and an economy model on at least 200 labelled questions per task family (FR-MDL-08): accuracy per group, false-approve rate, schema validity, p95 latency, cost per decision, failure handling.

**Routing** (`htco_application.routing`):

- Every employee has a versioned `ModelRoutingPolicy`: allowed providers, required capabilities, default and maximum tier, per-task budget, maximum data class, fallback tiers, advisor usage. No policy names a model; the catalog maps tiers to verified provider models.
- `route` (one deterministic router for all employees): `NO_AI` for deterministic tasks; refuse data above the policy's class; raise the tier to the task's difficulty within the policy's maximum; choose the cheapest healthy model in the catalog that meets providers, capabilities, data class and budget; fall back through the policy's fallback tiers, never below the task's difficulty. The decision (with policy version) is recorded in `routing_decisions` (FR-MDL-05).
- Historical performance, error rates and evaluation scores feed the catalog's health and the policy reviews; the router itself stays deterministic. Jev may judge task difficulty when rules cannot.

## Consequences

- Positive: cheap structured decisions where they are safe; every model choice explainable per employee; providers swappable.
- Negative: two more configuration surfaces (advisor providers, routing policies) to review; benchmark effort before Jev is used.
- Follow-ups: catalog and gateway adapters with budgets (Phase 8); Jev adapter, benchmark harness and the Decision & Model Observatory (Phases 8 and 16).

## Validation

Scenario E (two employees, one router, different policies) and Scenario F (rules versus advisor versus fallback, schema validity, failure handling, no effect on authorization) pass with fakes now; Scenario F is repeated with real providers in the Phase 8 benchmark.

## Validation log

- 2026-10-14 — Router and advisor chain tested with fakes (`tests/application/test_model_router.py`, `test_decision_advisor.py`).
