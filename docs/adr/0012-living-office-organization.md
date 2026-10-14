# ADR-0012: The living office: 16 AI employees, automations, tools and engineering as separate concepts

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Supersedes:** the 18 logical agent roles of PRD §8 (version 1.0) and the agent list in SOFTWARE_DESIGN §9.1 (version 1.0)
- **Related:** [ADR-0013](0013-automation-engine.md) (automations), [ADR-0014](0014-tool-registry.md) (tools), [ADR-0015](0015-decision-advisor-and-routing.md) (Decision Advisor and per-employee routing), [ADR-0003](0003-agent-runtime-langgraph.md) (runtime)

## Context

The owner's architecture brief of 2026-10-14 redefines HTCO as **a living, autonomous business office**: AI employees, deterministic automations, reusable AI tools, and an engineering department that can maintain and improve the office itself. The first plan modelled the AI workforce as 18 logical roles, several of which were really deterministic work (the Publishing Agent, most of the Fulfillment Inspector) or narrow slices of one job (Competitor Research, Product Sourcer and Product Classifier). It had no first-class home for automations, the tool library, continuous supervision, optimization or self-improvement.

### Repository audit (2026-10-14, before this change)

| Area | State | Evidence |
|---|---|---|
| Phase 0 | Docs, repositories and design system done; owner prerequisites open | [Phase 0](../plan/phases/phase-00-groundwork.md), issue #8 |
| Phase 1 | Slices 1.1–1.4 done; 1.5, 1.6 and 1.7 done except their account-dependent parts (deploy, ruleset, real Sentry and alerts). All in open pull requests #1–#7 and #11, CI green | `tests/` (705 tests after this ADR), [phase 1](../plan/phases/phase-01-walking-skeleton.md) |
| Phase 2 | Policy engine v0 for human roles (slice 2.4 part 1), PR #10; no database yet (needs the Supabase project) | `htco_application.access.policy` |
| Phase 3 | Clients tooling, token pipeline and the web shell (slice 3.3 part 1), clients PRs #1–#4 | `htco-clients` |
| Database | No migrations yet | `supabase/` does not exist |
| Agent runtime | A placeholder package with the LangGraph dependency; import-linter already confines LangGraph to it | `packages/agent-runtime`, `pyproject.toml` contracts |

**Compatible with the new model, kept unchanged:** the modular monolith and its layer rules (ADR-0002), "Zero-AI first" and deterministic workers (PRD §5), the policy engine (deny by default), the Model Gateway with a versioned routing policy and budget ledger (ADR-0004), the `DecisionPort` and the 200-example benchmark rule (FR-MDL-08), inbox and outbox, the job queue port (ADR-0007), the API, observability and CI built in Phase 1.

**Incompatible assumptions found:** the 18-role list (PRD §8, SOFTWARE_DESIGN §9, PERMISSION_MATRIX §2, phase files 8–17, GLOSSARY, COST_MODEL); "agents are named by their role, never given human names" (clients brand book); a single `worker` container that also hosts agents; an office view that only shows agent states; the Optimization Officer as a Phase 17 afterthought; no automation registry, tool registry, activity record model or engineering request flow.

## Decision drivers

- Employees, automations and tools are different things with different costs, risks and owners; mixing them hides AI spend and blurs accountability.
- The organization must not depend on LangGraph: the runtime is replaceable (Claude Agent SDK, Claude Managed Agents, a hybrid).
- Everything critical must be observable from the CRM, and supervised by an independent function, without a model reading every event.
- The office must improve itself, but never bypass review, CI, approvals or deployment gates.
- Preserve completed work; change only what the new model needs.

## Options considered

1. **Keep 18 roles, add automations and tools around them.** Least churn, but keeps duplicated roles and AI where code suffices.
2. **The brief's model: 16 employees in seven departments, plus a shared Decision Advisor, with Automation, Tools and Engineering as first-class departments.** More concepts, each with one clear job.
3. **One generalist agent with many tools.** Cheap to start, impossible to evaluate, budget or supervise per responsibility.

## Decision

Option 2.

### Four concepts that never mix

| Concept | What it is | Where it lives |
|---|---|---|
| **AI Workforce** | Employees with permanent identities and versioned profiles (goals, responsibilities, tools, automations, routing policy, autonomy, memory, evaluations). Work that needs judgment, interpretation or language. | `htco_domain.workforce`, `registry/employees/`, runtime adapters |
| **Automation Department** | Deterministic workflows triggered by events, schedules, authorized agent tools, user actions or other workflows. No model tokens unless a named step needs reasoning. | `htco_domain.automation`, `htco_application.automation`, `registry/automations/` ([ADR-0013](0013-automation-engine.md)) |
| **AI Tools Department** | A versioned library of capabilities with least-privilege access. Tools never decide objectives. | `htco_domain.tooling`, `registry/tools/` ([ADR-0014](0014-tool-registry.md)) |
| **Engineering & Infrastructure** | Develops, debugs and improves the platform, the employees and the office UI through engineering requests, pull requests, CI and controlled releases. | Alex, Amir and Iris; engineering requests (Phase 19) |

### The organization

| Department | Employees |
|---|---|
| Executive Office | **Maya** General Manager · **Noa** Quality & Operations Supervisor · **Omer** Continuous Optimization Officer · **Lia** Owner Assistant |
| Product Research, Content & Catalog | **Shon** Product Intelligence · **Noam** SEO & Copywriting · **Nika** Creative & Media · **Daniel** Catalog & Pricing Architect |
| Procurement & Finance | **Tom** Procurement & Supplier Quotes · **Roni** Finance Controller |
| Order Assurance | **Eitan** Fulfillment Inspector |
| Marketing | **Gal** Marketing & Growth |
| Customer Service | **Dana** Customer Service |
| Engineering & Development | **Alex** Lead Software & Platform Engineer · **Amir** Debugger & Reliability Engineer · **Iris** Agent Systems & Experience Engineer |

Plus **Jev**, a shared Decision Advisor service with an identity and activity history in the office, but no department and no headcount ([ADR-0015](0015-decision-advisor-and-routing.md)).

### Migration from the 18 roles

| Old role (PRD 1.0) | New home |
|---|---|
| 01 General Manager / Orchestrator | Maya |
| 02 Quality Supervisor | Noa, now with deterministic monitors, the nightly supervisor and follow-up verification |
| 03 Optimization Officer | Omer, active from Phase 7 (pilot scope; full scope in Phase 14) with a measured proposal lifecycle |
| 04 Debugger | Amir (Engineering) |
| 05 Owner Assistant | Lia |
| 06 Competitor Research, 07 Product Sourcer, 08 Product Classifier | Shon (merged) |
| 09 SEO Copywriter | Noam |
| 10 Image Creator | Nika |
| 11 Catalog Architect, 12 Pricing Analyst | Daniel (merged) |
| 13 Publishing Agent | The **Catalog Draft Creation & Publishing** automation (not an employee) |
| 14 Supplier Quote Agent | Tom |
| 15 Fulfillment Inspector | Eitan for exceptions; routine processing is the Order Intake, Tracking Sync and Incident Detection automations |
| 16 Finance Controller | Roni; recording stays in the Finance Ledger Ingestion automation |
| 17 Customer Service | Dana |
| 18 Marketing | Gal |
| — (new) | Alex and Iris (Engineering), Jev (shared advisor) |

### Rules

1. **Configuration is data, versioned and reviewed.** Each employee, automation, tool and advisor is a document in `registry/` (database rows with versions from Phase 6). Every employee has an Agent Specification Sheet of 22 sections (`EMPLOYEE_SPEC_SECTIONS`); every automation a specification of 18 (`AUTOMATION_SPEC_SECTIONS`). Sections still being designed are marked `draft`.
2. **Configuration is not readiness.** An employee or automation becomes `ready` only with every section defined and a passed evaluation; the domain refuses anything else. Today all 16 employees, 15 automations and 40 tools are `draft`.
3. **Framework independence.** `EmployeeIdentity`, `EmployeeProfile`, `BusinessTask`, `AutomationDefinition`, `AutomationExecution`, `ToolDefinition`, `ModelRoutingPolicy`, `ActivityRecord` and `OptimizationProposal` are plain domain types. Runtimes implement the `AgentRuntime` port (`invoke`, `resume`, `interrupt`, `cancel`, `inspect`). LangGraph state and checkpoints are runtime concerns, never the source of truth for orders, money, products or identities. LangGraph orchestrates reasoning, not deterministic actions.
4. **The CRM is the single source of business truth.** Every meaningful action produces an activity record with organization, shop, actor (`user`, `employee`, `automation`, `advisor`, `system`), task, correlation and causation IDs and an outcome: business summaries, never private model reasoning.
5. **Supervision is layered.** Deterministic monitors check completeness, transitions, deadlines, heartbeats and queues (layer 1); Noa reviews exceptions and samples by risk (layer 2); scheduled daily and weekly reviews (layer 3); follow-up verification (layer 4). A finished conversation never proves a finished task.
6. **Self-improvement is gated.** Omer proposes with baselines and acceptance criteria; Maya prioritizes; Alex or Iris implement through pull requests; CI, review and the owner's release gate apply; Omer measures. No employee may weaken tests, remove logging, widen its own permissions or bypass branch protection.
7. **Deployment stays small.** `api`, `agent-worker` (employees and, until load says otherwise, automations) and `web`. No container per employee. An `automation-worker` splits out only when isolation or measured load justifies it (an ADR).

### Migration classification (existing work)

| Class | Items |
|---|---|
| **Keep unchanged** | Phase 1 skeleton (API, problems, logs, tracing, error reporting, CI, image, hooks, contract); clients tooling, tokens, web shell; ADR-0001, 0002, 0005, 0006, 0008–0011 |
| **Extend** | Policy engine (human roles today; employee and automation actors join through `ActorRef` and tool access); Model Gateway (per-employee policies); `DecisionPort` → `DecisionAdvisor`; PERMISSION_MATRIX §2; event catalog; data model; Phase 2 (actors in audit, employee registry tables) |
| **Refactor** | PRD §1, §4, §6–§9, §11–§12; SOFTWARE_DESIGN §3, §4, §9, §12; MVP; phases 5–7 (automation engine, activity records); clients brand book on agent names; office UX |
| **Replace** | Phase files 8–17 by phases 8–21 of the new plan; PRD §8 role table |
| **Defer** | Storage and APIs for the registry (Phase 6), the durable automation engine (Phase 4), supervision (Phases 7 and 14), optimization (Phases 7 and 14), engineering requests (Phase 19) |
| **Remove after compatibility verification** | Nothing in code; the 18-role table stays in this ADR as history |

## Consequences

- Positive: AI cost and risk concentrate where judgment is needed; every concept has one owner and one test strategy; the organization survives a runtime change; supervision and optimization become measurable functions instead of prompts.
- Negative: more concepts and registry documents to maintain; employee names must be consistent across both repositories and the design system.
- Follow-ups: database tables and APIs for the registry (Phase 6); the activity record store (Phases 2 and 5); agent-by-agent design and evaluation before each activation; the clients' office information architecture (Phase 16).

## Validation

The foundation is tested now: framework independence (Scenario J), least-privilege tool and automation access (Scenario H), idempotent automation replays (Scenario G, in memory), per-employee routing through one router (Scenario E) and the Decision Advisor chain (Scenario F, with fakes). Scenarios A–D and I are scheduled in the phases that build their stores and workflows ([TESTING_STRATEGY §10](../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)).

## Validation log

- 2026-10-14 — Foundation merged with 705 passing tests; `registry/` validated: 16 employees in seven departments, Jev as an advisor, 15 automations, 39 tools (40 after adding the Listing Health write tool), none production-ready.
