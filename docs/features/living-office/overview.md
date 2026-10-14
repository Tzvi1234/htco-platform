# Living office — Overview

| | |
|---|---|
| **Slug** | `living-office` |
| **Phase / slices** | Foundation with Phase 2 (pure domain and application code); stores, runtimes and activations in Phases 5–19 ([PLAN §4](../../plan/PLAN.md#4-coverage-of-the-living-office-capabilities)) |
| **Requirements** | FR-AGT-01, 04, 09, 10; FR-AUT-01..05; FR-TOOL-01..02; FR-MDL-02, 09; FR-AUD-04; FR-OPT-02 ([PRD](../../product/PRD.md)) |
| **Decisions** | [ADR-0012](../../adr/0012-living-office-organization.md), [0013](../../adr/0013-automation-engine.md), [0014](../../adr/0014-tool-registry.md), [0015](../../adr/0015-decision-advisor-and-routing.md) |
| **Status** | Foundation complete; no employee, automation or tool is production-ready |
| **Last updated** | 2026-10-14 |

## What and why

HTCO is a living office: 16 AI employees, deterministic automations, versioned tools and an engineering department, plus the shared Decision Advisor. This feature is the foundation every later phase builds on: the organization as framework-independent domain types, the office as reviewed data in `registry/`, and the rules that keep the four concepts apart (least-privilege tool access, idempotent automation triggers, one router with a policy per employee, rules-first decisions).

## Acceptance criteria (foundation)

| ID | Criterion | Status |
|---|---|---|
| AC-LO.1 | `registry/` holds 16 employees in the seven departments of ADR-0012, Jev as an advisor (not an employee), the 15 initial automations and the initial tools; `just registry` validates it. | Pass |
| AC-LO.2 | An employee or automation cannot be `ready` or `active` without every specification section defined and (employees) a passed evaluation. | Pass |
| AC-LO.3 | Scenario H: an employee is refused any tool or automation outside its grants, and any tool while it is not production-ready. | Pass |
| AC-LO.4 | Scenario G (in memory): a replayed trigger never repeats the business effect; failures retry then dead-letter. | Pass |
| AC-LO.5 | Scenario E: two employees use one router with their own policies, data classes and budgets. | Pass |
| AC-LO.6 | Scenario F (fakes): rules decide first; providers are validated, recorded and fall back; advice never changes authorization. | Pass |
| AC-LO.7 | Scenario J: the business model imports without LangGraph and works through any `AgentRuntime`. | Pass |

## Scope

- **In:** `htco_domain` actors, workforce, routing, tooling, automation, tasks, activity, improvement, state machines; `htco_application` registry, tool access, automation engine (in memory), router, decision advisor, runtime port; `registry/`; `tools/registry.py`.
- **Out (planned):** database tables and APIs (Phases 5, 7, 8, 9, 10, 18, 19), the LangGraph adapter and tool bridge (8), the gateway and providers (9), supervision (10), every employee's completed specification and evaluation (its activation phase).

## Known limits

- The automation engine keeps executions and payloads in memory behind the `ExecutionStore` port until Phase 5.
- Specification sections are drafts; their text says what is still open.
