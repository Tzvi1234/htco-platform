# Phase 21 — Growth and scale

| | |
|---|---|
| **Milestone** | — (after M7 Release) |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 20](phase-20-hardening-release.md) |
| **Requirements** | FR-AGT-09, FR-MKT-01, FR-MKT-02, FR-SHOP-05 |
| **Mandatory tests owned** | None of the fifteen ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); re-runs test 1 for Shopify, test 12 for adopted routing changes, and tests 2, 3 and 15 after any worker or runtime change |
| **Status** | PLANNED |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 21.1 Gal activated: marketing and growth
- [ ] 21.2 Model and prompt A/B tests
- [ ] 21.3 Multi-shop operations
- [ ] 21.4 Shopify adapter
- [ ] 21.5 Worker split and runtime options, if measurements require them

## Goal

Grow sales and run more shops without cloning the system. **Gal** (Marketing & Growth) analyzes store and product performance and proposes authorized campaigns and experiments with Noam and Daniel; routes and prompts improve through controlled A/B tests; a second shop and a second marketplace run on the same platform, the same 16 employees and the same automations.

Decision-model benchmarking (Jev) is in [Phase 8](phase-08-models-tools.md), and continuous optimization (unit costs, Omer) in [Phase 14](phase-14-governance.md).

## Why now

Growth work needs a dependable base: real orders, finance, governance and a production release ([Phase 20](phase-20-hardening-release.md)). Marketing touches buyers and money, so it comes last and starts at the lowest autonomy.

## Scope

**In:**

- Gal's activation: analysis from Etsy listing stats (basic views and favourites only, FR-MKT-01) and our own orders; campaign and experiment proposals that use the marketplace's own tools (sales, coupons); no buyer data turned into a marketing list (FR-MKT-02); a consent ledger for any channel that needs consent.
- Routing and prompt experiments on the same evaluation sets, with guardrails and automatic rollback, recorded in `routing_decisions` and adopted only through Omer's proposal lifecycle and a versioned routing approval (mandatory test 12 still applies).
- Multi-shop onboarding with per-shop budgets, access, policies and shop-partitioned memory.
- A `ShopifyAdapter` behind `MarketplacePort` (FR-SHOP-05).
- Splitting `agent-worker` and `automation-worker`, or adding agent-worker instances, if measured load or isolation requires it (ADR with the measurements).
- If operating the Claude Agent SDK proves costly, a measured comparison with Claude Managed Agents or a Messages API adapter behind the same `AgentRuntime` port, as [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) foresees (ADR before any change).

**Out:** paid ads bought automatically (never without the owner's approval); new employees (a new employee is a registry document, an ADR if it changes the organization, and the full activation process); mailing lists built from Etsy buyer emails.

## Owner prerequisites

- Marketing policy: channels, budgets, what Gal may propose; consent requirements.
- Approval of Gal's autonomy level (proposed L0: suggests only).
- A second Etsy shop or a Shopify store for the multi-shop and marketplace pilots, and the matching developer access.
- Budget for experiments.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`; for client screens also `design-system`.
2. Read this file, then [PLAN.md](../PLAN.md), [PRD](../../product/PRD.md) FR-MKT-01, FR-MKT-02 and FR-SHOP-05, [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), the registry file of Gal, and the feature docs of the router, optimization and marketplace adapters.
3. Verify at kickoff: Etsy's current listing-stats and marketing APIs, Shopify's Admin API version and webhook rules, and the consent rules for any marketing channel. Record them with dates.
4. Order: 21.1, then 21.2, then 21.3 and 21.4 (they can run in parallel), and 21.5 only when measurements call for it.
5. Activate Gal with the activation process of [PLAN §1](../PLAN.md#1-how-a-phase-works): specification, least privilege, dataset, benchmark, dry run, red team, review, staging, approved activation.
6. Never use buyer contact data for marketing, never spend money or publish a campaign without an approval, and never change domain code to fit a marketplace (the adapter absorbs the differences).
7. Stop and ask the owner when: a marketing action needs consent or money; a marketplace API does not support what the plan assumes; an A/B test regresses quality or cost beyond its guardrail.

## Employees activated

| Employee | Registry |
|---|---|
| Gal, Marketing & Growth | [`gal.toml`](../../../registry/employees/gal.toml) |

## Slices

### 21.1 — Gal activated: marketing and growth

| ID | Acceptance criterion |
|---|---|
| AC-21.1.1 | Gal's specification sheet is fully defined, the evaluation passed, the autonomy approved by the owner and the routing measured (FR-AGT-09). |
| AC-21.1.2 | No buyer data appears in any marketing list or prompt (test over Gal's tools and outputs); consent is recorded in the ledger for any channel that needs it. |
| AC-21.1.3 | Every campaign or experiment is a proposal that goes through approvals; Gal cannot publish or spend. |

### 21.2 — Model and prompt A/B tests

| ID | Acceptance criterion |
|---|---|
| AC-21.2.1 | Experiments compare routes or prompts on the same evaluation sets, with cost and quality per item. |
| AC-21.2.2 | A regression beyond the guardrail rolls the experiment back automatically and records why. |
| AC-21.2.3 | A winning variant is adopted only through a verified optimization proposal and a versioned routing approval with evaluations. |

### 21.3 — Multi-shop operations

| ID | Acceptance criterion |
|---|---|
| AC-21.3.1 | A second shop connects with no new services or employees. |
| AC-21.3.2 | Per-shop budgets, access, policies and memory partitions are enforced; no data crosses shops (isolation tests through the API, realtime channels, tools and employee messages). |

### 21.4 — Shopify adapter

| ID | Acceptance criterion |
|---|---|
| AC-21.4.1 | Domain code is unchanged; the adapter passes the `MarketplacePort` contract tests with recorded fixtures. |
| AC-21.4.2 | The order automations run unchanged on Shopify events, and a webhook duplicated three times creates one order with the right unit count (mandatory test 1 for the new marketplace). |

### 21.5 — Worker split and runtime options

| ID | Acceptance criterion |
|---|---|
| AC-21.5.1 | Any split or runtime change is recorded in an ADR with the measurements that justify it. |
| AC-21.5.2 | After a change, the Phase 6 proof-of-concept gates and mandatory tests 2, 3 and 15 pass again. |

## Test plan

Gal's evaluation and red-team set (no buyer data, no unapproved spend); A/B test harness on evaluation sets; multi-shop isolation tests; Shopify adapter contract tests with recorded fixtures; mandatory test 1 for Shopify; regression run of the mandatory tests after any worker or runtime change.

**Mandatory tests (PLAN §5).** This phase owns none of the fifteen. It re-runs test 1 for Shopify, test 12 for adopted routing changes, and tests 2, 3 and 15 after any worker or runtime change.

## Security checklist

- [ ] No buyer contact data in marketing tools, lists or prompts; consent ledger in place.
- [ ] Per-shop isolation tested for every new shop and marketplace.
- [ ] Shopify credentials stored and rotated like Etsy's; webhooks signed and deduplicated.
- [ ] Threat model updated for marketing and the new marketplace.

## Documentation

`docs/features/employees/gal.md`, `docs/features/marketing/`, `docs/features/marketplaces/shopify.md`, `docs/features/multi-shop/`; ADRs for any worker split or runtime change; COST_MODEL (experiments, second shop); THREAT_MODEL; PLAN status.

## Demo

Gal proposes a sale for a slow product with evidence from listing stats and orders; the owner approves it and it runs through Etsy's own tools. Show an A/B test result on a route. Show a second shop in the shop switcher with its own budget, and a Shopify order flowing through the same automations.

## Exit gate

- [ ] Gal is `ready` and approved; a measured growth experiment completed.
- [ ] A second shop or marketplace live with no change to domain code.
- [ ] A/B tests produced at least one verified improvement or a documented null result.
- [ ] The owner approved the result.

## Rollback

Gal's kill switch; experiments roll back automatically; a new shop or marketplace can be disconnected without affecting the first; worker or runtime changes revert by configuration.

## Risks

| Risk | Mitigation |
|---|---|
| Marketing misuses buyer data | Tool grants without buyer contact data; tests; consent ledger |
| A/B tests chase noise | Same evaluation sets, guardrails, minimum sample sizes set at kickoff |
| A second marketplace leaks into the domain | `MarketplacePort` contract tests; import-linter |

## Cost impact

Gal's model usage; experiment budgets set by the owner; a second shop's Etsy or Shopify fees; possibly one more worker if 21.5 splits it (verify sizes and prices at kickoff).
