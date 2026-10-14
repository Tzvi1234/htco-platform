# Phase 0 — Groundwork, decisions and Claude-Native audit

| | |
|---|---|
| **Milestone** | M0 — Audit and plan |
| **Repositories** | both |
| **Size** | S |
| **Depends on** | — |
| **Requirements** | FR-SUP-04 |
| **Mandatory tests owned** ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | None. This phase records which phase owns each of the 15 tests |
| **Status** | In progress |

## Progress

- [x] 0.1 Repositories and documentation baseline
- [x] 0.2 Design system v1
- [ ] 0.3 Etsy access spike (read-only, owner's account)
- [ ] 0.4 AutoVA fact check
- [ ] 0.5 Decisions closed
- [ ] 0.6 Claude-Native audit and migration plan (recorded 2026-10-14 below; owner review open)

## Goal

Turn the original planning brief into an approved, verified starting point: repositories, documentation, design system, decisions, accounts, and facts about Etsy and AutoVA checked against reality, so that Phase 1 starts with no open blockers. Since the owner chose the Claude-Native Hybrid Edition, this phase also records the current state of the work and the plan for moving it to the Claude runtime without deleting anything (master plan milestone 0, "Audit / ADR").

## Why now

Every later phase depends on decisions made here (region, access tiers, catalog model) and on accounts that take time to approve (Etsy app, Anthropic API organization, code-signing identity later). Finding out in [Phase 9](phase-09-etsy-connection.md) that webhooks are unavailable, or in [Phase 12](phase-12-product-studio.md) that the catalog model conflicts with Etsy policy, would be expensive. The runtime change is cheapest to plan now, while the LangGraph work is still a placeholder and a port.

## Scope

**In:** private repositories with clean structure and docs; PRD, MVP, plan, software design, engineering rules, testing strategy, skills, ADRs; design system v1 (tokens and component guidelines); account setup; verification spikes that only read data; the Claude-Native audit of the current state and the migration plan; ADRs 0016–0018.

**Out:** application code, cloud resources that cost money before approval, any write to Etsy or AutoVA, deleting or rewriting existing work (LangGraph included).

## Owner prerequisites

| # | Task | Why |
|---|---|---|
| O-1 | Review and approve PRD, MVP, PLAN (version 3.0), SOFTWARE_DESIGN, ENGINEERING_RULES, the design system and ADRs 0016–0018 | They govern every phase |
| O-2 | Upgrade GitHub to Pro; decide on a machine account for agent pull requests (D-01, D-08) | Rulesets and required checks on private repos |
| O-3 | Choose the region (D-02) | Latency and data location |
| O-4 | Buy or confirm `ht-co.app`; choose DNS provider (A-01, D-11) | Domains for app, API and mail |
| O-5 | Create the Supabase organization and DigitalOcean team with 2FA, billing alerts and a spending cap | Ready for Phases 1–2 |
| O-6 | Register an Etsy Seller App for the pilot shop; note rate limits and whether a webhook portal appears (A-02, A-03, A-08) | Phase 4 fixtures and [Phase 9](phase-09-etsy-connection.md) design |
| O-7 | Ask AutoVA about an API, export or partner integration; confirm whether automatic ordering is active and whether tracking reaches Etsy for every order (A-05, A-06, A-07) | [Phases 10](phase-10-fulfillment-incidents.md) and [13](phase-13-procurement-finance.md) |
| O-8 | Read Etsy's current API Terms and Seller Policy (reselling, production partners, AI disclosure, photos) (A-09, D-04) | Product Studio and data retention |
| O-9 | Create the **Anthropic API organization** for HTCO, a dedicated **service account**, and **one API key per environment** (development first; staging and production only when those environments exist). Store each key as an encrypted DigitalOcean secret yourself; record the organization and service-account IDs (never keys) in ASSUMPTIONS. Set a monthly spend limit that matches decision D-06 ([ADR-0017](../../adr/0017-auth-and-billing-policy.md)) | Hosted runs use `API_PRODUCTION` only; needed by [Phase 6](phase-06-workforce-claude-runtime.md) |
| O-10 | Check whether your Claude plan includes **API credits**, and how to apply them, **only from Anthropic's official documentation and Console**. Do not connect a personal subscription, its login or a session cookie to any HTCO worker or screen | Credits may lower API cost; a subscription is never product infrastructure ([ADR-0017](../../adr/0017-auth-and-billing-policy.md)) |
| O-11 | Create an **OpenAI API key** (service key, own spend limit) **only when the Creative Studio is built** in [Phase 12](phase-12-product-studio.md). The Phase 8 adapter is built on recorded fixtures without a key | No unused paid key ([ADR-0018](../../adr/0018-image-generation-specialist.md)) |
| O-12 | Sign up for **Jev** only after its API, terms, pricing and our benchmark dataset have been reviewed in [Phase 8](phase-08-models-tools.md) (A-17, A-18) | No paid dependency before it is proven |
| O-13 | Create a **Supabase Pro project for development first** (the region of D-02; IPv4 add-on; settings listed in [Phase 2](phase-02-identity-tenancy.md)). **No production project** until the RTO and RPO are decided and a restore check has passed ([Phase 4](phase-04-durable-core-order-intake.md) slice 4.12) | Master plan: backup is not PITR; restore must be proven before real orders |
| O-14 | Create the DigitalOcean App Platform app **`htco-dev`** first, with `api` and `web`; add `agent-worker` only after the queue and the API are verified ([Phase 4](phase-04-durable-core-order-intake.md)). Create **`htco-prod`** only after the security approvals of [Phase 20](phase-20-hardening-release.md) | Pay only for what runs; no production before it is safe |

Changes that need the owner's explicit approval at any time (getting-started guide §10): buying a service or accepting a monthly cost; creating an API key that costs money; a production or sensitive database change; running on a subscription on an external server (forbidden without written approval from Anthropic, [ADR-0017](../../adr/0017-auth-and-billing-policy.md)); any flow with real money, a new Etsy listing or a risky customer message.

## Instructions for the agent

1. Load the skills `software-engineering` and `feature-docs`; load `feature-verification` for the spikes (0.3, 0.4). In `htco-clients`, also `design-system`.
2. Read [PLAN.md](../PLAN.md) (all of it), ADRs [0012](../../adr/0012-living-office-organization.md), [0016](../../adr/0016-claude-native-agent-runtime.md), [0017](../../adr/0017-auth-and-billing-policy.md), [0018](../../adr/0018-image-generation-specialist.md), [ASSUMPTIONS](../../product/ASSUMPTIONS.md) and both repositories' `CLAUDE.md`.
3. Work in this order: keep 0.1 and 0.2 as they are (done); support the owner on O-1 to O-14; run 0.3 when the Seller App key exists and record the fixtures that [Phase 4](phase-04-durable-core-order-intake.md) needs (receipts, and webhook deliveries if the portal exists), scrubbed and kept outside the repository until Phase 4 reviews them; run 0.4 with the owner's AutoVA answers; close 0.5; keep 0.6 current until the owner approves it.
4. For the audit (0.6), inspect the repository; do not trust the plan's own status text. Report each item as done, partial or missing, with the evidence (file, test, pull request).
5. Never in this phase: delete or rewrite existing work, LangGraph included; create cloud resources that cost money; create, read or store an API key, token or password (the owner does this); make a write call to Etsy or AutoVA; connect a personal Claude subscription to anything.
6. Stop and ask the owner when: a fact contradicts an ADR (for example Etsy webhooks are unavailable, or AutoVA does not write tracking to Etsy); a decision in ASSUMPTIONS is needed by Phase 1 or 2 and is still open; an account step needs payment or acceptance of terms.

## Slices

### 0.1 — Repositories and documentation baseline

- Private repositories `htco-platform` and `htco-clients` with README, CLAUDE.md, CONTRIBUTING, SECURITY, LICENSE (proprietary), CODEOWNERS, PR and issue templates.
- Product, plan, architecture, engineering and ADR documents; feature documentation template; skills for software engineering, TDD, feature verification and feature docs.
- **Acceptance:** AC-0.1.1 every document is linked from a docs index; AC-0.1.2 no file in either repository is a scratch note, session log, empty placeholder or duplicate; AC-0.1.3 links resolve.

### 0.2 — Design system v1

- Tokens in DTCG format derived from the official logo's measured colours (navy `#030F1F`, turquoise `#05F7F4`, amber `#F8B857`), light and dark themes with WCAG-checked contrast, type, spacing, radius, elevation and motion; component inventory and guidelines; a browsable design system reference.
- **Acceptance:** AC-0.2.1 `node design/tokens/check-contrast.mjs` passes: every text token meets 4.5:1 and every control boundary, focus ring, icon and status dot meets 3:1 on every background it may appear on, in both themes (202 pairs); AC-0.2.2 the official logo file is used unmodified.

### 0.3 — Etsy access spike (read-only, owner's account)

- With the Seller App's key: authorize the pilot shop with PKCE from a local script, read the shop, list the last 20 receipts with shipments, read the rate-limit headers, list listings with inventory. Check the webhook portal. Keep the raw responses (outside the repository) as the source of the Phase 4 fixtures.
- **Acceptance:** AC-0.3.1 a short report in `docs/features/etsy-integration/overview.md` records scopes granted, rate limits, webhook availability, the webhook signature scheme, shop region and whether receipts carry AutoVA's tracking; AC-0.3.2 no write call is made; AC-0.3.3 the script and its tokens never enter the repository; AC-0.3.4 the recorded responses are handed to Phase 4 for scrubbing, with the recording date and API version.

### 0.4 — AutoVA fact check

- Owner provides AutoVA's answers (O-7); compare 20 recent orders: tracking present in Etsy, timing from payment to tracking.
- **Acceptance:** AC-0.4.1 ADR-0009 and ASSUMPTIONS updated with the answers; AC-0.4.2 a first SLA window proposal per shipping method (median and 90th percentile time to tracking).

### 0.5 — Decisions closed

- D-01, D-02, D-03, D-05, D-07, D-08, D-11 decided and recorded; ADR-0005 accepted with the chosen region.
- **Acceptance:** AC-0.5.1 no "Open" decision in ASSUMPTIONS is needed by Phase 1 or 2.

### 0.6 — Claude-Native audit and migration plan

The getting-started guide's first step: inspect what exists (Phase 1, the LangGraph foundations, the documents and the database), record it, and plan the move to the Claude runtime. Nothing is deleted. ADRs [0016](../../adr/0016-claude-native-agent-runtime.md), [0017](../../adr/0017-auth-and-billing-policy.md) and [0018](../../adr/0018-image-generation-specialist.md) and PLAN version 3.0 are the decisions that come out of it.

**Acceptance:** AC-0.6.1 the current-state report below lists every area as done, partial or missing, with evidence; AC-0.6.2 the migration plan names, for every existing piece, whether it is kept, moved or extended, and in which phase; AC-0.6.3 no existing file, test or pull request is removed by the plan; AC-0.6.4 the owner has reviewed the report and the plan (O-1).

#### Current state (audit of 2026-10-14)

Inspected: `htco-platform` at the documentation branch (commits up to the living-office foundation, PR #12) and `htco-clients` (PRs #1–#4). Pull requests are open until the owner merges them.

| Area | State | Evidence |
|---|---|---|
| Workspace and tooling (1.1) | Done | `uv` workspace with `packages/domain`, `packages/application`, `packages/platform`, `packages/contracts`, `packages/agent-runtime`, `apps/api`; Ruff, mypy strict, import-linter, pytest with AnyIO, coverage floors; `justfile` |
| API skeleton (1.2) | Done | `apps/api` (`htco_api`): health and readiness, request IDs, problem+json, JSON logs, security headers; `tests/api/` |
| OpenAPI contract (1.3) | Done | `packages/contracts/openapi.yaml`, snapshot test, Schemathesis (`tests/contract/`) |
| CI (1.4) | Done | `.github/workflows/ci.yml` with a single `ci-gate`; actions pinned by SHA (`tools/check_action_pins.py`); test-integrity and Conventional Commits checks |
| Container and deployment (1.5) | Partial | `Dockerfile` (non-root, tested in `tests/infra/`) and its CI job exist. No `infra/digitalocean/` app spec, no deploy workflow, no deployed app: BLOCKED on the DigitalOcean account (O-5, O-14) |
| Agent guardrails and rules (1.6) | Partial | `.claude/settings.json` hooks and `tools/claude_hooks.py` done; the `main` ruleset is BLOCKED on GitHub Pro (O-2) |
| Monitoring and tracing (1.7) | Partial | Tracing, trace IDs in logs, error-reporting port with the Sentry adapter, diagnostic endpoint (`packages/platform`); real Sentry project and DigitalOcean alerts BLOCKED on accounts |
| LangGraph foundations | Partial (placeholder) | `packages/agent-runtime` (`htco_agent_runtime`) holds only the pinned `langgraph>=1.2,<1.3` dependency and a docstring; `tests/agent_runtime/test_langgraph_compat.py` runs a two-node graph on Python 3.14 (A-10 verified); import-linter contract `langgraph-only-in-agent-runtime`. No graphs, no checkpoint schema, no tool bridge |
| Runtime port | Done (port only) | `htco_application.agents.runtime`: `AgentRuntime` (`invoke`, `resume`, `interrupt`, `cancel`, `inspect`), `AgentSession`, `SessionStatus`; Scenario J in `tests/application/test_framework_independence.py` |
| Living-office domain | Done (no database) | `htco_domain` (`actors`, `workforce`, `tasks` with 13 `TaskState` values, `automation`, `activity`, `improvement`, `routing`, `tooling`); `htco_application` (`access.policy`, `automation.engine`, `workforce.registry`, `workforce.tool_access`, `routing.router`, `decisions.advisor`), tested with fakes |
| Registry | Partial | `registry/employees/` 16 profiles, all `draft`, without a `runtime` field; `registry/automations/` 15 specifications (the three new ones of v3 are missing: `autova-fulfillment-watch`, `product-metrics-rollup`, `agent-mail-alias-routing`); `registry/tools/` 12 families; `registry/advisors/jev.toml` |
| Policy engine v0 (2.4, part 1) | Done (pure) | `htco_application.access.policy`, `tests/application/test_access_policy.py` (PR #10) |
| Database | Missing | No `supabase/` directory, no migrations, no Supabase project: slices 2.1–2.3, 2.5, 2.6 BLOCKED on O-13 |
| Workers | Missing | No `workers/` directory; the queue and worker are [Phase 4](phase-04-durable-core-order-intake.md) |
| Documents | Done, being renumbered | PRD, MVP, software and code design, data model, event catalog, permission matrix, threat model, engineering rules, testing strategy, ADRs 0001–0015, feature docs for `platform-skeleton`, `identity-access`, `living-office`; ADRs 0016–0018 and PLAN 3.0 added; the other documents are renumbered to version 3.0 in the same documentation change |
| Clients | Partial | `htco-clients`: tooling and CI (3.1), token pipeline (3.2), web shell part 1 (3.3) in PRs #1–#4; design system v1 docs and tokens; no screen spec for the 43 screens yet |
| Subscription risk | None found | No code reads a Claude login, OAuth token or session cookie; no worker exists yet. ADR-0017 fixes the rule before any runtime code |

#### Migration plan (nothing is deleted)

| Existing piece | Decision | Phase |
|---|---|---|
| `AgentRuntime` port and Scenario J | Kept unchanged; the Claude adapter implements it | [6](phase-06-workforce-claude-runtime.md) |
| `packages/agent-runtime` (`htco_agent_runtime`, LangGraph pin, compat test) | Moved by a refactor-only slice to `packages/agent-runtime/langgraph/` (`htco_agent_runtime_langgraph`), behaviour unchanged; stays the alternative adapter | [6](phase-06-workforce-claude-runtime.md) |
| New `packages/agent-runtime/claude/` (`htco_agent_runtime_claude`) | Added, with an import-linter contract confining `claude_agent_sdk` to it | [6](phase-06-workforce-claude-runtime.md) |
| `registry/employees/*.toml` | Kept; `runtime` field added (default `claude`); profiles stay `draft` until each activation phase | [6](phase-06-workforce-claude-runtime.md), then 7 onwards |
| `registry/automations/` | Kept; three new specifications added as drafts in the phase that activates them | [10](phase-10-fulfillment-incidents.md), [14](phase-14-governance.md), [15](phase-15-mail-support.md) |
| Domain `TaskState` (13 states) | Kept; the eight UI statuses (IDLE, QUEUED, RUNNING, WAITING_EXTERNAL, WAITING_APPROVAL, BLOCKED, FAILED, OFFLINE) are derived from it and from agent runs, not stored separately | [5](phase-05-tasks-messages-approvals.md), [7](phase-07-pilot-office.md) |
| Automation engine (in memory) | Kept; Postgres store and job queue underneath | [4](phase-04-durable-core-order-intake.md) |
| Model router and Decision Advisor (pure, with fakes) | Kept; moved behind `packages/model-router` with real adapters; GPT images as a specialist adapter | [7](phase-07-pilot-office.md) (one Anthropic route), [8](phase-08-models-tools.md) |
| Phase 1 to 3 slices and pull requests | Kept with their numbers | 1–3 |
| Version 2.0 phase files 4–21 | Replaced by the version 3.0 files; the old ones stay in Git history | — |
| ADR-0003 (LangGraph) | Amended by ADR-0016, not superseded | — |
| TOOLCHAIN entries for LangGraph | Kept; `claude-agent-sdk` added after its minimum release age | [6](phase-06-workforce-claude-runtime.md) |

Debts found: the deployment, ruleset and database are blocked on owner accounts (O-2, O-5, O-13, O-14); every document that cites a phase number (PRD phase column, DATA_MODEL, EVENT_CATALOG, TESTING_STRATEGY §10, registry drafts, the clients navigation map) must follow the version 3.0 numbering; a link and phase-number check runs before the owner reviews this phase.

## Test plan

Documentation checks: every relative link in both repositories resolves, and the token contrast check (`htco-clients/design/tokens/check-contrast.mjs`) passes all 202 pairs (run on 2026-10-14). Phase 3 runs it in CI. The audit's evidence column is checked by opening each named file and test.

## Security checklist

- [ ] No secrets, tokens or personal data in any committed file (scan both repositories).
- [ ] Etsy spike tokens stored only on the owner's machine and revoked after the spike if not reused.
- [ ] GitHub, Supabase, DigitalOcean, Anthropic and (later) OpenAI accounts have 2FA enabled.
- [ ] Anthropic API keys exist only as DigitalOcean secrets, one per environment, created by the owner; only their IDs are recorded.
- [ ] No personal Claude subscription, login or session cookie is connected to any HTCO component.

## Documentation

All documents created in slice 0.1; this phase's results recorded in ASSUMPTIONS, ADR-0005, ADR-0008 and ADR-0009; the audit and migration plan in slice 0.6 above; ADRs 0016–0018; PLAN version 3.0.

## Demo

Walk the owner through the plan, the PRD, the MVP, the design system, the audit and migration plan, the Claude-native ADRs and the open decisions; agree on the answers.

## Exit gate

- [ ] Owner approved the documents, ADRs 0016–0018 and the audit (O-1, AC-0.6.4).
- [ ] Prerequisites O-2 to O-6 and O-13 done; O-9 done before [Phase 6](phase-06-workforce-claude-runtime.md); O-7 and O-8 at least in progress.
- [ ] Etsy spike report merged.
- [ ] Phase 1 has no open blocker.

## Rollback

Nothing deployed. Documents are versioned in git.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy Seller App unavailable or slow for the shop | Fall back to a Personal App application; [Phase 4](phase-04-durable-core-order-intake.md) runs on fixtures and [Phase 9](phase-09-etsy-connection.md) uses whichever key exists |
| Catalog model conflicts with Etsy policy | Decision D-04 before [Phase 12](phase-12-product-studio.md); Phases 1–11 do not depend on it |
| A personal subscription ends up in the product by mistake | ADR-0017; the Claude runtime refuses to start without `API_PRODUCTION` (Phase 6 tests) |
| The audit misses existing work and it is rebuilt | Evidence column per area; nothing is deleted, so a missed piece can still be reused |

## Cost impact

GitHub Pro about $4/month; domain registration (yearly). Supabase Pro for the development project from [Phase 2](phase-02-identity-tenancy.md). Anthropic API usage starts in [Phase 6](phase-06-workforce-claude-runtime.md) under the D-06 cap; no OpenAI or Jev spend before Phases 12 and 8. No hosting cost in this phase.
