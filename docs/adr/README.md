# Architecture Decision Records

An ADR records one decision that someone will later ask "why did we do it this way?" about: a technology choice, a boundary, a trade-off. We use a short form of [MADR 4](https://adr.github.io/madr/).

## When to write one

- Adding or replacing a technology, service, provider or major library.
- Changing a module boundary, a contract strategy or the deployment shape.
- Accepting a known trade-off or risk on purpose.
- Deviating from the [engineering rules](../engineering/ENGINEERING_RULES.md) or from an earlier ADR.

Accepted ADRs are not rewritten. Two things may be added later: a status change, and dated entries in the ADR's **Validation log** recording evidence (spike results, gate outcomes, facts verified with real accounts). A changed decision gets a new ADR that supersedes the old one and links back.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-two-repositories.md) | Two repositories: platform and clients | Accepted |
| [0002](0002-modular-monolith-hexagonal.md) | Modular monolith with hexagonal boundaries | Accepted |
| [0003](0003-agent-runtime-langgraph.md) | LangGraph agent runtime behind a port | Accepted (validated by the Phase 8 gates) |
| [0004](0004-model-gateway.md) | Own model gateway on official SDKs | Accepted |
| [0005](0005-hosting-digitalocean-supabase.md) | DigitalOcean App Platform and Supabase Pro | Proposed (accepted with the region decision) |
| [0006](0006-web-auth-bff.md) | Web authentication through a backend-for-frontend | Accepted |
| [0007](0007-job-queue.md) | Postgres-native job queue behind a port | Proposed (Phase 5 spike) |
| [0008](0008-etsy-integration.md) | Etsy integration: Seller App, webhooks plus reconciliation, read-first tracking | Accepted (validated in Phases 0 and 4) |
| [0009](0009-autova-integration.md) | AutoVA behind a port, tracking read from Etsy | Accepted |
| [0010](0010-test-first-development.md) | Test-first development with mechanical enforcement | Accepted |
| [0011](0011-data-access-and-migrations.md) | psycopg with explicit SQL; Supabase CLI migrations as the single schema source | Accepted |

Client decisions live in `htco-clients/docs/adr/`.

## Template

```markdown
# ADR-NNNN: <decision as a short phrase>

- **Status:** Proposed | Accepted | Superseded by ADR-XXXX
- **Date:** YYYY-MM-DD
- **Deciders:** <names or roles>

## Context
What problem are we solving, and what forces are at play?

## Decision drivers
- <the criteria that matter most>

## Options considered
1. <option> — <one-line summary of pros and cons>

## Decision
What we chose, stated plainly.

## Consequences
- Positive: …
- Negative: …
- Follow-ups: …

## Validation
How and when we will know the decision was right (a test, a spike, a metric, a phase gate).

## Validation log
- YYYY-MM-DD — evidence and outcome.
```
