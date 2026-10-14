# Phase 21 — Growth and scale

| | |
|---|---|
| **Milestone** | **M8 — Scale** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 20](phase-20-hardening-launch.md) |
| **Requirements** | FR-AGT-09, FR-MKT-01, FR-MKT-02, FR-SHOP-05 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 21.1 Gal activated: marketing and growth
- [ ] 21.2 Model A/B tests for routes and prompts
- [ ] 21.3 Multi-shop operations
- [ ] 21.4 Shopify adapter
- [ ] 21.5 Worker split if measurements require it

## Goal

Grow sales and run more shops without cloning the system. **Gal** (Marketing & Growth) analyzes store and product performance and proposes authorized campaigns and experiments with Noam and Daniel; routes and prompts are improved by controlled A/B tests; a second shop and a second marketplace run on the same platform.

Decision-model benchmarking moved to [Phase 9](phase-09-router-advisor.md) and continuous optimization (unit costs, FinOps views, Omer) to [Phase 18](phase-18-optimization.md) in version 2.0 of the plan.

## Scope

**In:** Gal's activation (analysis from listing stats and orders; campaign and experiment proposals through the marketplace's own tools; no buyer data turned into marketing lists; consent ledger for any channel that needs it); routing and prompt experiments on the same evaluation sets with guardrails and automatic rollback, recorded in `routing_decisions` and proposed through Omer's lifecycle; multi-shop onboarding with per-shop budgets and access; a `ShopifyAdapter` behind `MarketplacePort`; splitting `agent-worker` into `agent-worker` and `automation-worker` if measured load or isolation requires it (ADR).

**Out:** paid ads bought automatically (never without the owner's approval); new employees (a new employee is a registry document, an ADR if it changes the organization, and the full activation rule).

## Owner prerequisites

- Marketing policy: channels, budgets, what Gal may propose; consent requirements.
- Approval of Gal's autonomy level (proposed L0: suggests only).
- Second shop or Shopify store for the pilot.

## Employees activated

| Employee | Registry |
|---|---|
| Gal, Marketing & Growth | [`gal.toml`](../../../registry/employees/gal.toml) |

Gal follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 21.1 | Gal activated: performance analysis, campaign and experiment proposals | Sheet defined, evaluation passed, autonomy approved, routing measured; no buyer data in marketing lists; proposals go through approvals |
| 21.2 | Model A/B tests for routes and prompts | Experiments compare on the same evaluation sets; automatic rollback on regression; adopted only through a verified optimization proposal |
| 21.3 | Multi-shop operations | A second shop connects with no new services or employees; per-shop budgets and access enforced |
| 21.4 | Shopify adapter | Domain code unchanged; adapter contract tests pass; the order automations run unchanged on Shopify events |
| 21.5 | Worker split if measurements require it | Decision recorded in an ADR with the measurements |

## Exit gate — Milestone M8

- [ ] Gal is `ready` and approved; a measured growth experiment completed.
- [ ] A second shop or marketplace live with no change to domain code.
- [ ] A/B tests produced at least one verified improvement or a documented null result.

## Cost impact

Gal's model usage; experiment budgets set by the owner; a second shop's Etsy or Shopify fees.
