# Phase 17 — Optimization and scale

| | |
|---|---|
| **Milestone** | **M8 — Scale** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 16 |
| **Requirements** | FR-MDL-08, FR-SHOP-05 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 17.1 Experiment framework for routes and prompts
- [ ] 17.2 Decision-model benchmark and `DecisionPort` adapters
- [ ] 17.3 Optimization Officer
- [ ] 17.4 Multi-shop operations
- [ ] 17.5 Shopify adapter
- [ ] 17.6 FinOps dashboards

## Goal

Spend less per unit of work and run more shops without cloning the system: model A/B testing, decision-model benchmarks, multi-shop operations, a second marketplace adapter, FinOps dashboards and the Optimization Officer.

## Scope

**In:** routing experiments with guardrails and rollback; benchmark of typed-decision providers (TypeSafe Jev, OpenAI Decisions) against rules and small models on 200+ labeled cases per decision type (accuracy per group, false-approve rate, p95 latency, cost per decision); Optimization Officer agent (L0, recommendations backed by experiments); multi-shop onboarding and per-shop budgets; `ShopifyAdapter` behind `MarketplacePort`; FinOps dashboards (cost per unit by agent, shop and product); splitting the worker into `automation-worker` and `agent-worker` if measurements require (ADR).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 17.1 | Experiment framework for routes and prompts | Experiments compare on the same eval sets; automatic rollback on regression |
| 17.2 | Decision-model benchmark and `DecisionPort` adapters | Adopted only if it beats the baseline on accuracy and cost |
| 17.3 | Optimization Officer | Recommendations cite experiment data; no policy changes without approval |
| 17.4 | Multi-shop operations | A second shop connects with no new services or agents |
| 17.5 | Shopify adapter | Domain code unchanged; adapter contract tests pass |
| 17.6 | FinOps dashboards | Cost per unit visible and attributable |

## Exit gate — Milestone M8

- [ ] Measured cost reduction per unit with no quality regression; a second marketplace or shop live.
