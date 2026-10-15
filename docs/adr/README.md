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
| [0003](0003-agent-runtime-langgraph.md) | LangGraph agent runtime behind a port | Accepted (validated by the Phase 8 gates); scope narrowed by 0012 and 0013 |
| [0004](0004-model-gateway.md) | Own model gateway on official SDKs | Accepted; amended by 0015 |
| [0005](0005-hosting-digitalocean-supabase.md) | DigitalOcean App Platform and Supabase Pro | Accepted (Frankfurt, GHCR) |
| [0006](0006-web-auth-bff.md) | Web authentication through a backend-for-frontend | Accepted |
| [0007](0007-job-queue.md) | Postgres-native job queue behind a port | Proposed (Phase 5 spike) |
| [0008](0008-etsy-integration.md) | Etsy integration: Seller App, webhooks plus reconciliation, read-first tracking | Accepted (validated in Phases 0 and 4) |
| [0009](0009-autova-integration.md) | AutoVA behind a port, tracking read from Etsy | Accepted |
| [0010](0010-test-first-development.md) | Test-first development with mechanical enforcement | Accepted |
| [0011](0011-data-access-and-migrations.md) | psycopg with explicit SQL; Supabase CLI migrations as the single schema source | Accepted |
| [0012](0012-living-office-organization.md) | The living office: 16 AI employees, automations, tools and engineering as separate concepts | Accepted; supersedes PRD 1.0 §8 (18 roles) |
| [0013](0013-automation-engine.md) | Automation registry and workflow engine independent of the agent runtime | Accepted |
| [0014](0014-tool-registry.md) | Central tool registry with capability-level, least-privilege access | Accepted |
| [0015](0015-decision-advisor-and-routing.md) | Shared Decision Advisor; one router with a policy per employee | Accepted; amends 0004 |
| [0016](0016-web-first.md) | Web first: every feature works on the web before any native app | Accepted |
| [0017](0017-engineering-on-claude-code-subscription.md) | The Engineering department runs on Claude Code with the owner's subscription | Accepted |

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
