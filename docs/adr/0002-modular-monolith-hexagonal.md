# ADR-0002: Modular monolith with hexagonal boundaries

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The platform covers ten bounded contexts (identity, shops, orders and fulfillment, catalog, sourcing, finance, support, AI workforce, incidents, media and marketing), several external integrations and an agent runtime. It will be built over many months, largely by AI coding agents, by a very small team. Without enforced structure, such a codebase erodes quickly.

## Decision drivers

- Business rules must stay independent of frameworks, the database, Etsy and the agent runtime.
- Boundaries must be enforced by tools, not by memory or discipline.
- Operating cost and complexity must stay low for the pilot.

## Options considered

1. **Microservices per context.** Strong isolation, but many deployables, network failure modes and costs we do not need yet.
2. **A layered monolith without enforcement.** Fast at first, then tangled.
3. **A modular monolith with hexagonal (ports and adapters) boundaries, enforced in CI.**

## Decision

Option 3, with this package layout inside one `uv` workspace:

| Package | Role | May import |
|---|---|---|
| `htco_domain` | Entities, value objects, policies, state machines, domain events | Standard library only |
| `htco_application` | Use cases, ports, unit of work, policy engine | `htco_domain` |
| `htco_persistence` | Postgres adapters, inbox, outbox, job queue, audit | application, domain, platform, database driver |
| `htco_integrations` | Etsy, AutoVA, email adapters | application, domain, platform, httpx, provider SDKs |
| `htco_model_router` | Model gateway, routing, provider adapters | application, domain, platform, model SDKs |
| `htco_agent_runtime` | LangGraph adapter, graphs, tool bridge | application, domain, platform, LangGraph |
| `htco_platform` | Settings, logging, tracing, crypto, clock, IDs | Standard library and infrastructure libraries |
| `htco_api`, `htco_worker` | Composition roots and transport | Everything above |

The original brief's folder list (`apps/api`, `packages/domain`, `packages/contracts`, `packages/integrations`, `packages/model-router`, `packages/agent-runtime`, `workers/…`) is kept; `packages/application`, `packages/persistence` and `packages/platform` are added so that use cases, database code and cross-cutting infrastructure each have exactly one home.

## Consequences

- Positive: domain logic is testable in milliseconds; adapters are swappable (Shopify, another model provider, another agent runtime); AI agents get clear rules about where code belongs.
- Negative: more packages and mapping code than a quick prototype; contributors must learn the dependency rule.
- Follow-ups: import-linter contracts in Phase 1; a split into separate deployables only through a new ADR with evidence (load, isolation or team boundaries).

## Validation

Architecture tests fail the build on any forbidden import. Review after Phase 11 whether any module boundary is causing repeated friction (phases renumbered by PLAN 2.0 on 2026-10-14).
