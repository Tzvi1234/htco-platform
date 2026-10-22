# Phase 8 — Workforce runtime and tool registry

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (workforce directory, kill switches) |
| **Size** | L |
| **Depends on** | [Phase 7](phase-07-tasks-approvals.md); runs in parallel with [Phase 8A](phase-08a-browser-infrastructure.md) and [Phase 8B](phase-08b-authenticator.md) |
| **Requirements** | FR-ADM-03 (kill switches), FR-AGT-01, FR-AGT-02, FR-AGT-03, FR-AGT-04, FR-AGT-05, FR-AGT-06, FR-AGT-07, FR-AGT-09, FR-AGT-10, FR-AUT-02, FR-IAM-07, FR-TOOL-01, FR-TOOL-02, FR-TOOL-03 |
| **Status** | Not started |

## Progress

- [ ] 8.1 Registry tables and reviewed sync
- [ ] 8.2 Registry API (read)
- [ ] 8.3 Agent runtime (LangGraph adapter)
- [ ] 8.4 Tool bridge and tool invocation records
- [ ] 8.5 Internal messages and handoffs through the CRM
- [ ] 8.6 Kill switches
- [ ] 8.7 Evaluation harness
- [ ] 8.8 Proof-of-concept gates
- [ ] 8.9 Workforce directory UI
- [ ] 8.10 Tool bridge on the Action Gateway

**Foundation already in code.** The organization exists as plain domain types and pure rules, without a database: `htco_domain.workforce` (`EmployeeIdentity`, `EmployeeProfile`, the 22 `EMPLOYEE_SPEC_SECTIONS`, `AutonomyLevel`), `htco_domain.tooling` (`ToolDefinition`), `htco_application.workforce.registry` (loads and validates [`registry/`](../../../registry)), `htco_application.workforce.tool_access` (`decide_tool_access`), `htco_application.automation.engine` (employee triggers through `agent_tool`) and the `AgentRuntime` port in `htco_application.agents.runtime` (`invoke`, `resume`, `interrupt`, `cancel`, `inspect`). Tests: `tests/application/test_registry.py`, `test_tool_access.py`, `test_framework_independence.py`, `tests/domain/test_workforce.py`, `tests/tools/test_registry_files.py`. `packages/agent-runtime` is a placeholder with the pinned LangGraph dependency; import-linter already confines LangGraph to it. Everything below that is not listed here is planned.

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- **The tool bridge is a client of the Action Gateway.** Slice 8.4's checks (`decide_tool_access`, the policy engine, approvals, the outbox, `tool_invocations`) run inside the gateway's algorithm, not in a second implementation. The bridge adds only the session's identity and the interrupt and resume plumbing.
- **The LangGraph runtime uses the Postgres checkpointer** (`AsyncPostgresSaver`, `agent_runtime` schema; ADR-0003). A session waiting on an approval or a Human-channel task is interrupted and resumed from its checkpoint. Checkpoints never hold business state.
- **Employees reach every channel only through the gateway:** API tools, browser actions (Phase 8A, Browser Use bindings allowed for employees only), Internal and Human.
- **Etsy write tools** granted in the registry become approvals at every autonomy level (ADR-0022 rule 6). The write executes under the approval's execution identity, never inside the session.

## Goal

The office can run AI employees safely, before any employee is switched on: the organization (departments, employees, profile versions, tools, automations) lives in Postgres with versions, loaded from the reviewed `registry/` documents; an employee's reasoning runs through the `AgentRuntime` port, implemented with LangGraph (durable checkpoints, interrupts for approvals and long waits, versioned graphs); every tool call passes the tool bridge, which enforces `decide_tool_access` and the policy engine and records a `tool_invocations` row; employees hand off work through CRM tasks and messages; each employee has its own kill switch. The six proof-of-concept gates from the original brief pass as automated tests with fixture employees.

**No employee is activated in this phase.** All 16 employees stay `draft`; the first activation is Eitan in [Phase 9](phase-09-router-advisor.md), after the model router and budgets exist.

## Why now

Phases 5–7 gave the office its deterministic core: automations, tasks, approvals, activity records. Employees are the riskiest actors (they read untrusted content and call tools), so the controls that contain them (registry, tool bridge, kill switches, evaluation harness) must exist and be proven before the first one does real work.

## Scope

**In:** registry tables (`departments`, `employees`, `employee_profile_versions`, `tool_definitions` with versions, version history for `automation_definitions`, `evaluation_runs`) loaded from `registry/` by a reviewed migration or sync step; read-only registry API; the LangGraph adapter of the `AgentRuntime` port (`agent_runtime` checkpoint schema, `agent_sessions`, `agent_threads`, resume through jobs, graph versioning); the tool bridge (`decide_tool_access` plus the policy engine, run identity, outbox for external effects, `tool_invocations`, rate limits); internal messages and handoffs through the CRM (`office_messages`); professional memory written only by validated code paths (`agent_memory`); kill switches per employee and per automation, plus the global one; evaluation harness; proof-of-concept gates; Scenario H end to end and Scenario J; workforce directory UI.

**Out:** real model calls, the model catalog, budgets and routing records ([Phase 9](phase-09-router-advisor.md); this phase uses a fake gateway behind a `ModelGateway` port); the Decision Advisor adapters (Phase 9); employee activation (Phase 9 onwards); the office experience ([Phase 15](phase-15-office-executive.md)); incident remediation by Amir ([Phase 19](phase-19-engineering.md)).

## Owner prerequisites

- D-03 decided (first content workflow: Listing Health in [Phase 11](phase-11-listing-health.md)).
- Review of the department and employee names as they will appear in the product (the registry documents are the source).

## Slices

### 8.1 — Registry tables and reviewed sync

Migrations for `departments`, `employees` (identity, department, current profile version, kill switch), `employee_profile_versions` (autonomy, readiness, tools, automations, routing policy, the 22-section specification, evaluation result), `tool_definitions` (one row per tool version: family, owner, side effect, risk, data scope, audit requirement, readiness) and a version history for the `automation_definitions` of Phase 5. `htco registry sync` reads `registry/` through `htco_application.workforce.registry`, shows a dry-run diff, and is applied by the owner-started migration workflow; a change in the documents creates a new version row, never an update in place. Readiness is copied only after the domain check (`EmployeeProfile` and `AutomationDefinition` refuse `ready` with a `draft` section or without a passed evaluation).

| ID | Acceptance criterion |
|---|---|
| AC-8.1.1 | After a sync, every department, employee, profile, tool and automation in `registry/` is in the database with the registry commit it came from, and CI fails when the seeded database and `registry/` differ. |
| AC-8.1.2 | Changing one employee document produces exactly one new `employee_profile_versions` row; the previous version stays readable and sessions keep the version they started with. |
| AC-8.1.3 | A document that marks an employee or automation `ready` while any specification section is `draft` or the evaluation has not passed is rejected by the sync, and nothing is written. |
| AC-8.1.4 | No application role can insert, update or delete registry rows outside the sync; no employee tool can change its own profile, grants, autonomy or routing policy (grants test over every registered tool). |
| AC-8.1.5 | The sync refuses a grant of an unknown tool or an automation not callable from `agent_tool` (the registry's existing validation, now enforced against the database). |

### 8.2 — Registry API (read)

`GET /v1/office/departments`, `GET /v1/employees`, `GET /v1/employees/{employee_id}` (current profile with readiness, autonomy, tools, automations, routing policy summary, evaluation status), `GET /v1/employees/{employee_id}/versions`, `GET /v1/tools`, `GET /v1/tools/{name}/versions`, `GET /v1/automations/{automation_id}/versions`; cursor pagination; contract-first in `openapi.yaml`. Read-only: changes go through `registry/` and review.

| ID | Acceptance criterion |
|---|---|
| AC-8.2.1 | Every route is in the OpenAPI contract and passes Schemathesis. |
| AC-8.2.2 | Viewer and Support roles see identities and readiness but not specification sections marked internal or routing budgets (role matrix test). |
| AC-8.2.3 | There is no write route for registry data; a `POST`, `PUT`, `PATCH` or `DELETE` on these paths returns 405. |

### 8.3 — Agent runtime (LangGraph adapter)

The LangGraph adapter of the `AgentRuntime` port in `packages/agent-runtime` (`htco_agent_runtime`); `agent_runtime` schema migration for checkpoints, writes, blobs and store; strict msgpack and an encrypted serializer; `agent_sessions` (employee, profile version, task, runtime name, status, correlation ID) and `agent_threads` (lock, `graph_version`, status); sessions started and resumed by jobs on the Phase 5 queue; durability `sync` for graphs that wait for long periods; recursion limit 25–50; checkpoint retention job. LangGraph orchestrates reasoning only: deterministic actions are automations triggered through the `automation.trigger` tool, and business state stays in the CRM tables, never in checkpoints (ADR-0012 rule 3).

| ID | Acceptance criterion |
|---|---|
| AC-8.3.1 | A session interrupted for approval resumes after a worker restart from its checkpoint, and its `agent_sessions` row moves `running → interrupted → running → completed`. |
| AC-8.3.2 | Two resume jobs for the same thread never run concurrently; the second is a no-op. |
| AC-8.3.3 | A fixture checkpoint from the previous graph version resumes correctly after a compatible change; an incompatible change is refused with a clear error and the session is escalated, not corrupted. |
| AC-8.3.4 | `cancel` stops a session within one step and records the cancellation; `inspect` returns status, employee, profile version and task without exposing model reasoning. |
| AC-8.3.5 | **Scenario J** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): the same business task runs through the LangGraph adapter and through a second, plain-Python adapter of the port, producing the same CRM records (task transitions, activity records, tool invocations); the domain and application packages import nothing from LangGraph (import-linter), and `tests/application/test_framework_independence.py` keeps passing. |

### 8.4 — Tool bridge and tool invocation records

Every tool call from a session goes through one bridge: it takes the employee, tenant and acting user from the authenticated session (never from model arguments), calls `decide_tool_access` (unknown employee or tool, not ready, not granted, approval for critical tools and for external or financial tools below L3) and then the policy engine of Phase 7; `REQUIRE_APPROVAL` creates an approval and interrupts the session; external effects go through the outbox; each call writes one `tool_invocations` row (employee, profile version, tool name and version, decision with reason, outcome, latency, cost) and one activity record. Per-tool rate limits and timeouts come from the tool definition. Professional memory (`agent_memory`) is written only through validated code paths that set source, organization, shop, confidence and expiry.

| ID | Acceptance criterion |
|---|---|
| AC-8.4.1 | **Scenario H, end to end**: a fixture employee whose model output requests a tool outside its grants (and, separately, an automation outside its `allowed_automations`) is denied by the bridge, the denial is recorded in `tool_invocations` and as an activity record with outcome `denied`, and nothing executes. The pure tests in `tests/application/test_tool_access.py` and `test_automation_engine.py` keep passing. |
| AC-8.4.2 | A tool argument naming another shop or organization is ignored; the session's shop is used, and the attempt is flagged in the invocation record. |
| AC-8.4.3 | An external tool called by an employee at autonomy below L3 creates an approval and runs only after it is decided; a critical tool always needs approval, whatever the autonomy. |
| AC-8.4.4 | Text from tool results or web pages cannot create or change memory, grants or instructions; memory items without source and expiry are rejected. |
| AC-8.4.5 | A tool that exceeds its rate limit or timeout fails with a typed error recorded in the invocation, and the session follows the task's retry rules. |

### 8.5 — Internal messages and handoffs through the CRM

Employees hand off work by creating or updating tasks with resource references and by sending internal messages (`messages.send_internal`, stored in `office_messages`), never by passing conversation state between sessions (FR-AGT-07). A handoff names the sender, the receiver (an employee, a department or a person), the task, and the resources it refers to.

| ID | Acceptance criterion |
|---|---|
| AC-8.5.1 | A handoff from one fixture employee to another creates a task assigned to the receiver and one message, both linked by correlation ID; the receiving session starts from the task and its resources, with no access to the sender's checkpoint. |
| AC-8.5.2 | A message cannot carry authority: a message asking the receiver to use a tool outside its grants is denied by the bridge like any other request. |
| AC-8.5.3 | Every message is a business statement with a summary length limit; messages are visible in the task's activity trail. |

### 8.6 — Kill switches

A kill switch per employee (on `employees`), per automation (on `automation_definitions`) and a global one, each changed only by the Owner or an Admin with an `aal2` session and audited. A switched-off employee starts no session, its running sessions are interrupted at the next step, and its pending tool calls are refused; a switched-off automation refuses new triggers and lets running executions finish or cancels them, as its specification says.

| ID | Acceptance criterion |
|---|---|
| AC-8.6.1 | Switching off one employee stops its new sessions within 60 seconds and leaves other employees running. |
| AC-8.6.2 | The global switch stops all new sessions within 60 seconds, and the state is shown in the workforce directory and the API. |
| AC-8.6.3 | A switched-off automation refuses triggers from every source (event, schedule, user, employee, workflow) with a recorded reason. |
| AC-8.6.4 | Every switch change writes one audit record with actor, target, before and after. |

### 8.7 — Evaluation harness

Dataset format (versioned, no real buyer data), runner, metrics (accuracy, false-approve rate, schema validity, cost, p95 latency), `evaluation_runs` per employee and automation, nightly job with a report. An employee's evaluation result in its profile version comes only from a recorded run. Each activation phase builds that employee's evaluation set with this harness.

| ID | Acceptance criterion |
|---|---|
| AC-8.7.1 | An evaluation run produces a report comparing at least two routes on the same dataset, with cost per item. |
| AC-8.7.2 | A profile version can be marked as having passed only by referencing an `evaluation_runs` row that met its thresholds; the thresholds are part of the employee's specification (section `evaluation`). |
| AC-8.7.3 | Evaluation datasets are checked in CI for buyer PII and secrets; a dataset containing either fails. |

### 8.8 — Proof-of-concept gates (from the original brief)

The gates run with **fixture employees** (test profiles shaped like the real ones, never activated) and the fake model gateway.

| ID | Gate |
|---|---|
| POC-1 | A duplicated `order.paid` event produces one order and one downstream task, also when the task is triggered by a fixture employee through `automation.trigger`. |
| POC-2 | An approved external write (for example a listing update) is delivered exactly once when the worker is killed between the database commit and the delivery, and the pre-write re-read prevents a duplicate when the write had already landed. |
| POC-3 | A session of a procurement-shaped fixture employee waits for a supplier quote (interrupt) and resumes a (simulated) day later from a delayed job, with no model call while waiting. |
| POC-4 | Fixture employees shaped like the General Manager and the Debugger & Reliability Engineer cannot execute a tool outside their grants, even when the model insists (Scenario H). |
| POC-5 | A research task hands off between three fixture employees (research, copy, catalog) through CRM tasks and internal messages, with cost reported per step. |
| POC-6 | A prompt-injection payload in a supplier page fixture changes no tool permission, no memory item and no instruction, and causes no side effect. |

Each gate is an automated test in CI; all six must pass.

### 8.9 — Workforce directory UI

Read-only directory in the web app: departments, employees with readiness (`draft` or `ready`), autonomy, current profile version and kill switch state; tools with family, risk and readiness; automations with readiness and recent executions; owner and admin controls for the kill switches (step-up required). The full office experience is [Phase 15](phase-15-office-executive.md).

| ID | Acceptance criterion |
|---|---|
| AC-8.9.1 | A `draft` employee is never shown as working or available; states shown come only from recorded sessions and activity. |
| AC-8.9.2 | Kill switch controls are hidden for roles without the capability and require step-up for those with it. |

### 8.10 — Tool bridge on the Action Gateway

| ID | Acceptance criterion |
|---|---|
| AC-8.10.1 | Every tool call of a fixture session produces exactly one `action_executions` row and one `tool_invocations` row that reference each other. |
| AC-8.10.2 | A session calling an Etsy write tool is interrupted with `PENDING_APPROVAL`; after the decision, the write runs once outside the session and the session resumes with the verified result. |
| AC-8.10.3 | An architecture test proves that `htco_agent_runtime` imports no channel adapter (API, Browser) directly. |

## Test plan

Fake model gateway and fake runtime adapter for unit and graph tests; registry sync tests against the local stack; tool bridge matrix over every registered tool and every fixture employee; injection corpus; POC gate tests; checkpoint compatibility fixtures; evaluation harness nightly; Scenarios H and J in CI.

## Security checklist

- [ ] OWASP Agentic ASI01–ASI10 controls from the [threat model](../../architecture/THREAT_MODEL.md#3-agent-specific-risks-owasp-top-10-for-agentic-applications-2026) implemented and tested.
- [ ] Rule of Two applied to every graph (documented per graph).
- [ ] Tenant, shop and acting user come only from the authenticated session; never from model output.
- [ ] Registry rows changed only by the reviewed sync; no employee can widen its own grants.
- [ ] Checkpoints encrypted; strict msgpack on; no secret or buyer PII in checkpoints.

## Documentation

`docs/features/workforce-registry/`, `docs/features/agent-runtime/`, `docs/features/tool-registry/`; ADR-0003 and ADR-0014 validation logs; DATA_MODEL tables marked created; PERMISSION_MATRIX §2 checked against the registry grants.

## Demo

Show the workforce directory with all 16 employees in `draft`; change a document in `registry/`, run the sync dry-run and apply it, and show the new profile version; run a fixture employee that asks for a forbidden tool and show the denial in `tool_invocations` and the activity view; kill the worker during an interrupt and resume; switch one employee off.

## Exit gate

- [ ] POC-1 to POC-6 pass; all AC-8.x pass, including Scenario H end to end and Scenario J.
- [ ] Kill switches verified in production.
- [ ] No employee is `ready` (activation starts in Phase 9).

## Rollback

Global kill switch; employee features behind flags; registry and runtime tables are additive; the sync can re-apply the previous registry commit.

## Risks

| Risk | Mitigation |
|---|---|
| LangGraph upgrade churn | Pin; upgrade deliberately with checkpoint-compatibility tests; the port keeps a replacement possible (Scenario J) |
| Registry and database drift | Sync from one source, CI comparison, no write API |
| Tool bridge becomes a bottleneck | It is in-process and pure until the policy lookup; measured in the POC gates |

## Cost impact

No model spend (fake gateway). Possibly an `agent-worker` memory upgrade (about $12/month).
