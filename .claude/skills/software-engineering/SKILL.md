---
name: software-engineering
description: Mandatory preflight and quality bar for ANY change in htco-platform — code, migrations, contracts, infrastructure or docs. Load it before planning or editing anything in this repository, including small fixes. Covers how to scope a slice, where code belongs (hexagonal layers), security and data rules, the definition of done, and when to stop and ask the owner.
---

# Software engineering in htco-platform

You are working on a production system that handles money, buyer data, marketplace credentials and autonomous agents. Follow this skill for every task. The full rules are in `docs/engineering/ENGINEERING_RULES.md`; this skill is the operational checklist.

## 1. Preflight (before writing anything)

1. **Find the slice.** Locate the phase file in `docs/plan/phases/` and the slice you are implementing. If the work is not in a slice or an issue, stop and ask the owner where it belongs.
2. **Read the context.** The slice's acceptance criteria, the feature folder `docs/features/<feature>/` if it exists, the relevant section of `docs/architecture/SOFTWARE_DESIGN.md`, and any ADR the slice mentions.
3. **Restate the goal** in two or three sentences, including what is out of scope.
4. **Map the change.** List the bounded contexts, layers, contracts (OpenAPI, events, database) and permissions it touches. If it changes a boundary, adds a technology or contradicts an ADR, write a new ADR first and get the owner's agreement.
5. **Security and privacy impact.** Note: none, low, or needs a threat model (auth, money, PII, webhooks, agents, migrations, external writes all need one).
6. **Plan the tests** with the `tdd` and `testing` skills and the verification with the `feature-verification` skill before coding.
7. **Check the red lines** in [ENGINEERING_RULES §0](../../../docs/engineering/ENGINEERING_RULES.md#0-red-lines). If your plan needs to cross one, stop and ask the owner.

## 2. Where code belongs

The full map, with every package, port and folder, is [`docs/architecture/CODE_DESIGN.md`](../../../docs/architecture/CODE_DESIGN.md). The short version:

| You are writing… | It goes in | It may import |
|---|---|---|
| A business rule, calculation, state transition | `packages/domain` (`htco_domain`) | Standard library only |
| A use case, a port (interface), a policy decision | `packages/application` (`htco_application`) | `htco_domain` |
| SQL, repositories, inbox, outbox, job queue | `packages/persistence` | application, domain, platform, psycopg |
| Etsy, AutoVA, email code | `packages/integrations` | application, domain, platform, httpx, that provider's SDK |
| Model calls, routing, prices, budgets | `packages/model-router` (`htco_model_router`) | application, domain, platform, model SDKs |
| The Claude runtime adapter: session options, the `htco` MCP server, hooks, transcript mapping | `packages/agent-runtime/claude` (`htco_agent_runtime_claude`) | application, domain, platform, `claude-agent-sdk` |
| LangGraph graphs (alternative runtime) | `packages/agent-runtime/langgraph` (`htco_agent_runtime_langgraph`; today still `packages/agent-runtime`) | application, domain, platform, LangGraph |
| The tool bridge, task briefs, agent steps, billing policy | `packages/application` (`htco_application.tools`, `.agents`, `.billing`) | `htco_domain` |
| An employee, automation, tool or advisor definition | `registry/` (TOML), checked by `just registry` | — (data, reviewed like code) |
| Settings, logging, tracing, crypto, clock | `packages/platform` | infrastructure libraries |
| HTTP routes and schemas, DI wiring | `apps/api` | everything above |
| Job consumers and schedules | `workers/` | everything above |
| Schema changes | `supabase/migrations` (+ pgTAP in `supabase/tests`) | — |
| API and event contracts | `packages/contracts` | — |

Never put business logic in routers, consumers or SQL. Never return database rows or provider payloads across a boundary. Keep the four office concepts apart ([ADR-0012](../../../docs/adr/0012-living-office-organization.md)): judgment in employees, repeatable actions in automations, capabilities in tools, system changes through engineering.

## 3. Non-negotiables

- **Tenancy:** every business row has `organization_id`; commercial rows also `shop_id`. Authorization is decided on the server from the verified identity. Client-supplied IDs are lookups only.
- **Money:** `Decimal` and `NUMERIC(14,4)` with a currency. Never floats.
- **Time:** UTC `timestamptz`; inject the clock.
- **Idempotency:** every handler, job and webhook tolerates duplicates; external side effects go through the outbox with an idempotency key.
- **Secrets:** never in code, tests, fixtures, logs, prompts or commits.
- **Agents:** model calls only through the model gateway; tools only through the tool bridge and the policy engine; tenant and actor from the run, never from model output; Rule of Two.
- **Claude runtime:** API-key billing only for hosted runs; disposable sessions (one bounded step, state in Postgres); only granted `mcp__htco__*` tools, no built-in tools, `setting_sources=[]`, never `bypassPermissions` ([ADR-0016](../../../docs/adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../../docs/adr/0017-auth-and-billing-policy.md)).
- **Migrations:** forward-only, expand then contract, tested with pgTAP; never edit an applied migration; never apply to production yourself.
- **Errors:** typed domain errors; RFC 9457 problem responses; no stack traces to clients.
- **Logs:** structured, with `request_id` and `correlation_id`; no PII or secrets.

## 4. Implementation loop

Follow the `tdd` skill: one acceptance criterion at a time, failing test first, minimal code, refactor, full suite. Keep each pull request to one slice and ideally under 400 changed lines.

## 5. Before you say "done"

Run, and read the output of:

```
just check        # format, lint, types, architecture, unit and integration tests
just db-test      # pgTAP (when migrations or RLS changed)
just contract     # OpenAPI snapshot, breaking-change check, Schemathesis
```

(The command table in CLAUDE.md says which slice creates each command. If a command does not exist yet, say so instead of claiming it passed.)

Then:

1. Verify the feature for real with the `feature-verification` skill and record the evidence.
2. Update docs with the `feature-docs` skill: feature folder, contracts, DATA_MODEL, EVENT_CATALOG, PERMISSION_MATRIX, ADRs, CLAUDE.md or README if commands changed, and tick the slice in its phase file.
3. Fill in the pull request template honestly, including the security checklist with ASVS IDs and a "Test changes" section if any existing test changed.
4. Report to the owner in Hebrew: what changed, how it was verified, what is left, anything risky. Never say a check passed if you did not run it.

## 6. Refactoring

Refactor when it makes the current slice safer or simpler, with all tests green before and after. Larger refactors get their own slice and pull request. Never mix a refactor with a behaviour change in one commit.

## 7. Stop and ask the owner before

- applying a migration or changing data in production;
- spending money or creating paid resources;
- any write to Etsy, AutoVA or email with real data outside an approved test plan;
- changing permissions, security settings, secrets or the policy engine's rules;
- adding a dependency of a new kind, or any dependency without a clear reason;
- deviating from an ADR or these rules;
- deleting anything you did not create in this session.

## 8. Never

- weaken, skip or delete a test to make the build pass;
- disable a security check, hook or CI job;
- commit generated junk, scratch notes or session logs;
- invent API behaviour of Etsy, AutoVA or providers: check the docs or the recorded fixtures, and record new facts in the feature's `overview.md`.
