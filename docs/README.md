# HTCO OS Documentation

This folder is the system's source of truth: what we are building, in what order, how it is designed, and the rules we keep. Client-specific documentation (design system, UX, client architecture) lives in the `htco-clients` repository.

## Start here

| If you want to… | Read |
|---|---|
| Understand the product | [Product requirements (PRD)](product/PRD.md) |
| Know what the MVP is | [MVP definition](product/MVP.md) |
| See the build order and current phase | [Master plan](plan/PLAN.md) |
| Understand how the system is built | [Software design](architecture/SOFTWARE_DESIGN.md) |
| Know the rules for any change | [Engineering rules](engineering/ENGINEERING_RULES.md) |
| Write or review tests | [Testing strategy](engineering/TESTING_STRATEGY.md) |

## Map

| Area | Documents |
|---|---|
| **Product** | [PRD](product/PRD.md) · [MVP](product/MVP.md) · [Feature backlog](product/FEATURE_BACKLOG.md) · [Glossary](product/GLOSSARY.md) · [Assumptions and open decisions](product/ASSUMPTIONS.md) |
| **Plan** | [Master plan](plan/PLAN.md) · [Execution order](plan/EXECUTION_ORDER.md) · [Phases 0–21, 8A, 8B](plan/phases/) |
| **Architecture** | [Software design](architecture/SOFTWARE_DESIGN.md) · [Data model](architecture/DATA_MODEL.md) · [Event catalog](architecture/EVENT_CATALOG.md) · [Permission matrix](architecture/PERMISSION_MATRIX.md) · [Threat model](architecture/THREAT_MODEL.md) · [Execution engine](architecture/execution-engine.md) · [Etsy API capabilities](architecture/etsy-api-capabilities.md) · [Etsy multi-shop policy](architecture/etsy-multi-shop-policy.md) · [Etsy automation approval](architecture/etsy-automation-approval.md) · [AutoVA integration](architecture/autova-integration.md) |
| **Decisions** | [Architecture decision records](adr/README.md) |
| **Engineering** | [Engineering rules](engineering/ENGINEERING_RULES.md) · [Testing strategy](engineering/TESTING_STRATEGY.md) · [Toolchain](engineering/TOOLCHAIN.md) |
| **Features** | [Feature docs and template](features/README.md) |
| **Cost** | [Cost model](cost/COST_MODEL.md) |
| **Reviews** | [Post-merge review, 2026-10-23](reviews/2026-10-23-post-merge-review.md) |

## Documents created later, in the phase that needs them

| Document | Phase |
|---|---|
| `packages/contracts/openapi.yaml` | 1 |
| Event JSON Schemas in `packages/contracts/events/` | 5 |
| `docs/operations/RUNBOOK.md`, `docs/operations/SLO.md` | 16 (entries start in feature `operations.md` files from Phase 1) |
| Model catalog verification record | 8 |

## How these documents stay correct

Docs change in the same pull request as the code ([ER-DOC-01](engineering/ENGINEERING_RULES.md#10-documentation-er-doc)), every phase gate re-checks them, and nothing here is a scratch note: if a file is not linked from this page or a feature index, it should not exist.
