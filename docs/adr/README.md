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
| [0003](0003-agent-runtime-langgraph.md) | LangGraph agent runtime behind a port | Accepted (to be validated by the Phase 8 gates); scope narrowed by 0012 and 0013; amended by 0019 |
| [0004](0004-model-gateway.md) | Own model gateway on official SDKs | Accepted; amended by 0015 |
| [0005](0005-hosting-digitalocean-supabase.md) | DigitalOcean App Platform and Supabase Pro | Accepted (Frankfurt, GHCR) |
| [0006](0006-web-auth-bff.md) | Web authentication through a backend-for-frontend | Accepted |
| [0007](0007-job-queue.md) | Postgres-native job queue behind a port | Accepted (2026-10-22: in-house queue); amended by 0019 |
| [0008](0008-etsy-integration.md) | Etsy integration: Seller App, webhooks plus reconciliation, read-first tracking | Accepted (validated in Phases 0 and 4); amended by 0022 |
| [0009](0009-autova-integration.md) | AutoVA behind a port, tracking read from Etsy | Accepted |
| [0010](0010-test-first-development.md) | Test-first development with mechanical enforcement | Accepted |
| [0011](0011-data-access-and-migrations.md) | psycopg with explicit SQL; Supabase CLI migrations as the single schema source | Accepted |
| [0012](0012-living-office-organization.md) | The living office: 16 AI employees, automations, tools and engineering as separate concepts | Accepted; supersedes PRD 1.0 §8 (18 roles); amended by 0019 |
| [0013](0013-automation-engine.md) | Automation registry and workflow engine independent of the agent runtime | Accepted; amended by 0019 |
| [0014](0014-tool-registry.md) | Central tool registry with capability-level, least-privilege access | Accepted; amended by 0019 |
| [0015](0015-decision-advisor-and-routing.md) | Shared Decision Advisor; one router with a policy per employee | Accepted; amends 0004; amended by 0023 |
| [0016](0016-web-first.md) | Web first: every feature works on the web before any native app | Accepted |
| [0017](0017-engineering-on-claude-code-subscription.md) | The Engineering department runs on Claude Code with the owner's subscription | Accepted |
| [0018](0018-order-history-and-buyer-data.md) | Order history is stored with the buyer's name and shipping address, never contact data | Accepted |
| [0019](0019-unified-execution-engine-and-action-gateway.md) | One execution engine and one Action Gateway for automations and AI employees | Accepted (2026-10-23, slice 5.0 complete: AC-5.0.1 and AC-5.0.2 tested); amends 0003, 0007, 0012, 0013, 0014 |
| [0020](0020-per-shop-browser-environment.md) | An optional, honest per-shop Browser Environment (no anti-detect browser, no proxy per shop) | Accepted (owner, 2026-10-23) |
| [0021](0021-authenticator-service.md) | An internal Authenticator service: PyOTP on the existing vault | Proposed (Phase 8B) |
| [0022](0022-etsy-write-tools-and-browser-scope.md) | Every Etsy API endpoint as a tool, writes under approvals; browser actions only inside the recorded Etsy scope | Accepted (browser actions off until scoped); amends 0008 |
| [0023](0023-routing-modes-and-difficulty-assessment.md) | Per-employee routing mode (routed or pinned to a catalog alias); difficulty by rules, then benchmarked Jev, never above the cap | Accepted (Phase 9); amends 0015 |
| [0024](0024-knowledge-base-and-retrieval.md) | Shared knowledge base on pgvector in Supabase; structured tools, then hybrid search, then the model | Accepted (Phase 9, slice 9.10) |

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
