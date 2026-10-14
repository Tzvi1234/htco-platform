# Phase 6 — Workforce registry and Claude runtime

| | |
|---|---|
| **Milestone** | — (prepares **M2 — Workforce pilot**, which closes in [Phase 7](phase-07-pilot-office.md)) |
| **Repositories** | `htco-platform` (most work), `htco-clients` (workforce directory, kill switches) |
| **Size** | L |
| **Depends on** | [Phase 5](phase-05-tasks-messages-approvals.md) |
| **Requirements** | FR-ADM-03, FR-AGT-01, FR-AGT-02, FR-AGT-03, FR-AGT-04, FR-AGT-05, FR-AGT-06, FR-AGT-07, FR-AGT-08, FR-AGT-09, FR-AGT-10, FR-AUT-02, FR-IAM-07, FR-MDL-07, FR-TOOL-01, FR-TOOL-02, FR-TOOL-03 |
| **Status** | Not started |

## Progress

- [ ] 6.1 Registry tables, reviewed sync and the `runtime` field
- [ ] 6.2 Registry API (read)
- [ ] 6.3 Move LangGraph to `packages/agent-runtime/langgraph` (refactor only)
- [ ] 6.4 AuthAndBillingPolicy
- [ ] 6.5 ClaudeAgentRuntime adapter
- [ ] 6.6 MCP tool bridge, hooks and tool invocation records
- [ ] 6.7 Agent runs and cost entries
- [ ] 6.8 Provider failure, quota and fallback
- [ ] 6.9 Internal messages and handoffs between employees
- [ ] 6.10 Kill switches, runtime switch and billing-mode switch
- [ ] 6.11 Evaluation harness
- [ ] 6.12 Proof-of-concept gates POC-1 to POC-8
- [ ] 6.13 Workforce directory UI (clients)

Slice 6.3 is a pure move and lands before any adapter code, so the Claude package is born next to a LangGraph package with the same shape. Slice 6.4 lands before 6.5, because the adapter refuses to start a session without a billing decision.

**Foundation already in code.** `htco_domain.workforce` (`EmployeeIdentity`, `EmployeeProfile`, the 22 `EMPLOYEE_SPEC_SECTIONS`, `AutonomyLevel`), `htco_domain.tooling` (`ToolDefinition`), `htco_application.workforce.registry` (loads and validates [`registry/`](../../../registry)), `htco_application.workforce.tool_access` (`decide_tool_access`), `htco_application.automation.engine` (employee triggers through `agent_tool`), `htco_application.routing.router` and the `AgentRuntime` port in `htco_application.agents.runtime` (`invoke`, `resume`, `interrupt`, `cancel`, `inspect`). Tests: `tests/application/test_registry.py`, `test_tool_access.py`, `test_framework_independence.py`, `tests/domain/test_workforce.py`, `tests/tools/test_registry_files.py`. `packages/agent-runtime` (`htco_agent_runtime`) is a placeholder with the pinned LangGraph dependency; import-linter already confines LangGraph to it. From [Phase 5](phase-05-tasks-messages-approvals.md): `business_tasks`, `task_executions` with `state_summary`, the task dispatcher and its step-handler interface, the recovery scheduler, `employee_messages` and `handoff_task`, policy engine v1, approvals and `activity_records`. Everything else below is planned.

## Goal

The office can run AI employees safely, before any employee is switched on, on the runtime chosen in [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) and billed as [ADR-0017](../../adr/0017-auth-and-billing-policy.md) requires:

- the organization (departments, employees, profile versions with their `runtime` field, tools, automations) lives in Postgres with versions, loaded from the reviewed `registry/` documents;
- `ClaudeAgentRuntime` implements the `AgentRuntime` port with the Claude Agent SDK: one disposable session runs one bounded step of one task, starts from a task brief built from Postgres, ends on a terminal HTCO tool or a limit, and writes its outcome, state summary, costs and evidence in one transaction;
- every session is authorized by the `AuthAndBillingPolicy` (only `API_PRODUCTION` for hosted runs) and runs with locked-down options that CI checks;
- every tool call passes the MCP tool bridge (`PreToolUse` hook → `decide_tool_access` + policy engine; `PostToolUse` → `tool_invocations` + activity record);
- quota, 429 and outages are normal states: tasks wait, automations keep running;
- employees hand off work through tasks and `employee_messages`; kill switches stop new runs within 60 seconds;
- the evaluation harness exists, and the eight proof-of-concept gates of ADR-0016 pass in CI with fixture employees and **no real model call in CI**.

**No employee is activated in this phase.** All 16 employees stay `draft`. The first activations are the pilot (Maya, Shon, Noa, Omer) in [Phase 7](phase-07-pilot-office.md).

## Why now

Phase 5 gave the office durable tasks, a dispatcher, messages, approvals and one activity trail. Employees are the riskiest actors: they read untrusted content, call tools and spend money on every turn. The controls that contain them (registry, billing policy, locked session options, tool bridge, kill switches, evaluation harness) must exist and be proven before the pilot in Phase 7 makes the first real model call on business work.

## Scope

**In:**
- Registry tables (`departments`, `employees`, `employee_profile_versions` with `runtime`, `tool_definitions` with versions, version history for `automation_definitions`, `evaluation_runs`) loaded from `registry/` by a reviewed sync; read-only registry API.
- The `runtime` profile field (`"claude"` default, or `"langgraph"`) in `registry/employees/*.toml` and the database; the `AGENT_RUNTIME_ENABLED` setting.
- Refactor-only move of today's `packages/agent-runtime` (`htco_agent_runtime`) to `packages/agent-runtime/langgraph/` (`htco_agent_runtime_langgraph`).
- `AuthAndBillingPolicy` in `htco_domain.billing` and `htco_application.billing`, with the tests listed in ADR-0017.
- `ClaudeAgentRuntime` in `packages/agent-runtime/claude/` (`htco_agent_runtime_claude`): task brief, bounded steps, terminal tools, locked-down `ClaudeAgentOptions`, the employee step handler for the Phase 5 dispatcher, `agent_runs`.
- The in-process MCP server `htco` built per session from the profile's grants; `PreToolUse` and `PostToolUse` hooks; `tool_invocations`; professional memory written only through validated code paths (`agent_memory`).
- `model_usage` cost rows from the SDK result's usage data, with a reservation before the session and settlement after it.
- Provider failure handling: 429, budget exhaustion, outage (mandatory test 3).
- The employee side of messages and handoffs (`messages.send_internal`, `task.handoff`).
- Kill switches per employee, per automation, global, per runtime and per billing mode.
- Evaluation harness; proof-of-concept gates POC-1 to POC-8 as automated tests; Scenarios H and J end to end.
- Workforce directory UI in the web app.

**Out:**
- Real model calls in CI. CI uses a fake transport or recorded SDK message streams (see slice 6.5). The only real calls in this phase are the owner-approved transcript recordings of slice 6.5.
- The model catalog, the full router, budgets per organization and month, the AI cost screen and the routing console ([Phase 8](phase-08-models-tools.md)). In this phase the model and fallback come from a **static routing fixture** behind the routing port, and the reservation uses the per-task cap from the profile. Phase 7 adds a minimal gateway with one Anthropic route.
- The tool registry's MCP gateway for external tools and the GPT image adapter ([Phase 8](phase-08-models-tools.md), [ADR-0018](../../adr/0018-image-generation-specialist.md)).
- A full LangGraph adapter (checkpoint schema, threads). LangGraph stays as the alternative adapter: this phase moves it and keeps it compiling and tested; it is built out only when a profile selects `runtime = "langgraph"` (a later ADR decides).
- Employee activation (Phase 7 onwards); the office experience ([Phase 16](phase-16-web-office.md)); the engineering runner with built-in tools for Alex, Amir and Iris ([Phase 19](phase-19-engineering.md)).
- `SUBSCRIPTION_INTERACTIVE` and `APPROVED_SUBSCRIPTION_RUNTIME` as working modes in the product: the first is never reachable from the product, the second stays off behind a flag.

## Owner prerequisites

- The Anthropic API organization, a dedicated service account and one API key per environment exist (Phase 0, ADR-0017); their IDs, not keys, are recorded in [ASSUMPTIONS.md](../../product/ASSUMPTIONS.md). The development key is stored as an encrypted DigitalOcean secret.
- Approve a small, fixed spend cap for recording SDK transcripts in the development environment (slice 6.5). The amount is set by the owner; verify current prices at kickoff.
- Review the department and employee names as they will appear in the product (the registry documents are the source).
- Confirm that kill switches may be changed only by the Owner and Admins with step-up.

## Instructions for the agent

Follow these steps in order. Stop and ask the owner where step 10 says so.

1. Load the skills `software-engineering` (every task), `tdd` (before any production code or migration), `testing`, `feature-verification` (before claiming a slice works) and `feature-docs` (before opening each pull request). For slice 6.13 also load `design-system`. Load `claude-api` before touching the Claude Agent SDK, and re-read the official Agent SDK documentation ([overview](https://code.claude.com/docs/en/agent-sdk/overview), [Python reference](https://code.claude.com/docs/en/agent-sdk/python)) at kickoff: the SDK changes often.
2. Read this file, then [PLAN.md](../PLAN.md) (§4 steps 3 and 5, §5 tests 2, 3, 4, 9, 14 and 15), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) (all of it, especially "Locked-down session options" and "Validation"), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ADR-0012](../../adr/0012-living-office-organization.md), [ADR-0014](../../adr/0014-tool-registry.md), [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), [ADR-0003](../../adr/0003-agent-runtime-langgraph.md), [SOFTWARE_DESIGN §9](../../architecture/SOFTWARE_DESIGN.md#9-the-living-office-architecture), [THREAT_MODEL §3](../../architecture/THREAT_MODEL.md#3-agent-specific-risks-owasp-top-10-for-agentic-applications-2026), [ENGINEERING_RULES §8](../../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai) and [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios).
3. Read the Phase 5 code you build on: the dispatcher's step-handler interface, `task_executions.state_summary`, `handoff_task`, the policy engine, approvals and `ActivityPort`. Do not create parallel task or message models.
4. **Verify facts at kickoff and record them in ADR-0016's validation log:** the current `claude-agent-sdk` release and its minimum-release-age status in [TOOLCHAIN.md](../../engineering/TOOLCHAIN.md); the exact names of `ClaudeAgentOptions` fields, hook events and `ResultMessage` fields used below; the names of every built-in tool to put in `disallowed_tools`; which environment variables the bundled Claude Code binary reads for authentication; and **the SDK's testing seam** (for example a `Transport` abstraction that can replace the subprocess). Do not hard-code model IDs or prices anywhere.
5. Build the slices in the order of the Progress list. One slice, one pull request. Slice 6.3 changes no behaviour: its pull request contains only moves, renames and import updates, and every existing test passes unchanged.
6. For each slice: acceptance criteria as failing tests first, then code, then verify against the local Supabase stack, then docs.
7. CI never calls a model. The test job runs with network access to model providers blocked and no Anthropic key in its environment; a test that tries to reach the network fails.
8. Keep `claude_agent_sdk` imports inside `htco_agent_runtime_claude` and `langgraph` inside `htco_agent_runtime_langgraph` (import-linter). Domain and application code import neither.
9. Never in this phase: activate an employee or mark any profile `ready`; use a personal Claude login, subscription token or extracted OAuth credential anywhere; use `bypassPermissions`, `acceptEdits` or `auto` permission modes; load settings from the filesystem into a session; pass Supabase, Etsy, email or GitHub credentials into a session's environment; let a model argument choose the organization, shop or acting user; store or display model reasoning; rely on SDK session resume for correctness; keep any state on local disk beyond one session's temporary directory.
10. Stop and ask the owner when: a proof-of-concept gate cannot pass without changing ADR-0016 (for example the SDK has no usable testing seam, or `PreToolUse` cannot deny reliably); the measured memory per session needs a larger `agent-worker`; a transcript recording would exceed the approved cap; or the registry documents need business decisions (names, departments, grants).
11. At the end, run all POC gates and the exit gate checklist, update ADR-0016's validation log with the evidence, and prepare the demo.

## Slices

### 6.1 — Registry tables, reviewed sync and the `runtime` field

Migrations for `departments`, `employees` (identity, department, current profile version, kill switch), `employee_profile_versions` (runtime, autonomy, readiness, tools, automations, routing policy, task limits for `max_turns` and the per-step budget, the 22-section specification, evaluation result), `tool_definitions` (one row per tool version: family, owner, side effect, risk, data scope, audit requirement, readiness, whether it is terminal), a version history for the `automation_definitions` of Phase 4, and `evaluation_runs`. The `runtime` field is added to every `registry/employees/*.toml` (`"claude"` for all 16) and validated by `htco_application.workforce.registry`. The terminal tools of ADR-0016 (`task.complete`, `task.handoff`, `task.wait_for_approval`, `task.wait_for_event`, `task.escalate`) are added to `registry/tools/office_crm.toml` as draft tool definitions; their names are aligned with the existing `tasks.*` family at kickoff.

`htco registry sync` reads `registry/`, shows a dry-run diff, and is applied by the owner-started migration workflow; a change in the documents creates a new version row, never an update in place. Readiness is copied only after the domain check (`EmployeeProfile` and `AutomationDefinition` refuse `ready` with a `draft` section or without a passed evaluation).

| ID | Acceptance criterion |
|---|---|
| AC-6.1.1 | After a sync, every department, employee, profile, tool and automation in `registry/` is in the database with the registry commit it came from, and CI fails when the seeded database and `registry/` differ. |
| AC-6.1.2 | Changing one employee document produces exactly one new `employee_profile_versions` row; the previous version stays readable, and a task step keeps the profile version it was dispatched with. |
| AC-6.1.3 | A document that marks an employee or automation `ready` while any specification section is `draft` or the evaluation has not passed is rejected by the sync, and nothing is written. |
| AC-6.1.4 | No application role can insert, update or delete registry rows outside the sync; no employee tool can change its own profile, grants, autonomy, runtime or routing policy (grants test over every registered tool). |
| AC-6.1.5 | The sync refuses a grant of an unknown tool, an automation not callable from `agent_tool`, or a `runtime` value other than `claude` or `langgraph`; a profile without `runtime` gets `claude`. |

### 6.2 — Registry API (read)

`GET /v1/office/departments`, `GET /v1/employees`, `GET /v1/employees/{employee_id}` (current profile with runtime, readiness, autonomy, tools, automations, routing policy summary, evaluation status, kill switch state, derived status), `GET /v1/employees/{employee_id}/versions`, `GET /v1/tools`, `GET /v1/tools/{name}/versions`, `GET /v1/automations/{automation_id}/versions`; cursor pagination; contract-first in `openapi.yaml`. Read-only: changes go through `registry/` and review.

| ID | Acceptance criterion |
|---|---|
| AC-6.2.1 | Every route is in the OpenAPI contract and passes Schemathesis. |
| AC-6.2.2 | Viewer and Support roles see identities and readiness but not specification sections marked internal or routing budgets (role matrix test). |
| AC-6.2.3 | There is no write route for registry data; a `POST`, `PUT`, `PATCH` or `DELETE` on these paths returns 405. |

### 6.3 — Move LangGraph to `packages/agent-runtime/langgraph` (refactor only)

Move today's `packages/agent-runtime/` to `packages/agent-runtime/langgraph/`, rename the Python package `htco_agent_runtime` to `htco_agent_runtime_langgraph` and the distribution to `htco-agent-runtime-langgraph`, update the uv workspace members, the import-linter `root_packages` and layer contracts, the Dockerfile and the docs. No behaviour, dependency version or test changes.

| ID | Acceptance criterion |
|---|---|
| AC-6.3.1 | The pull request contains only moves, renames, import updates and configuration; the full test suite passes unchanged and `uv.lock` changes only in the package name. |
| AC-6.3.2 | import-linter confines `langgraph` to `htco_agent_runtime_langgraph`, and a contract forbids the two runtime packages from importing each other (a deliberate violation fails CI). |
| AC-6.3.3 | `tests/application/test_framework_independence.py` keeps passing; CLAUDE.md, README, SOFTWARE_DESIGN §4.2 and [ADR-0003](../../adr/0003-agent-runtime-langgraph.md) name the new path. |

### 6.4 — AuthAndBillingPolicy

The pure policy in `htco_domain.billing` (`BillingMode`: `API_PRODUCTION`, `SUBSCRIPTION_INTERACTIVE`, `APPROVED_SUBSCRIPTION_RUNTIME`; inputs: where the run happens (hosted worker or developer machine), the runtime, the enabled modes, the feature flag, the mode switches) and the decision use case in `htco_application.billing`, which the Claude adapter calls before every session. The decision returns the allowed mode and a reference to the credential (the name of the secret, never its value), or a refusal with a reason. The API key is read only inside the Claude adapter from the environment secret named by the decision.

| ID | Acceptance criterion |
|---|---|
| AC-6.4.1 | A hosted worker gets `API_PRODUCTION` and nothing else; a worker configured with `SUBSCRIPTION_INTERACTIVE` refuses to start any session, and the refusal is recorded (ADR-0017 validation test). |
| AC-6.4.2 | A session without an allowed mode is refused, the task moves to `WAITING_FOR_EXTERNAL` with the reason, and an admin alert is raised; nothing falls back silently. |
| AC-6.4.3 | `APPROVED_SUBSCRIPTION_RUNTIME` is refused while its feature flag is off, and the flag cannot be turned on by configuration alone in a hosted environment (a test asserts the flag's default and that turning it on requires the recorded ADR reference). |
| AC-6.4.4 | No API route, tool result, log line, prompt, activity record or error message contains the key; a log and response scanner test with a canary key passes. |
| AC-6.4.5 | The policy imports nothing from any SDK or framework (import-linter), and its decision table is covered by table-driven tests. |

### 6.5 — ClaudeAgentRuntime adapter

`ClaudeAgentRuntime` in `packages/agent-runtime/claude/` (`htco_agent_runtime_claude`) implements the `AgentRuntime` port and registers the `employee` step handler with the Phase 5 dispatcher for profiles whose runtime is `claude` and enabled.

**One session = one bounded step.**
- `invoke` and `resume` both start a new session from a **task brief** built by code from Postgres: the task, its inputs and resource references, the latest `task_executions.state_summary`, the employee's instructions from the profile version, the shop scope, and the step's limits. Buyer data is masked to the profile's maximum data class (FR-MDL-07). The brief is versioned and its hash is stored on `agent_runs`.
- The step ends when the employee calls a terminal HTCO tool (`task.complete`, `task.handoff`, `task.wait_for_approval`, `task.wait_for_event`, `task.escalate`), or reaches `max_turns`, `max_budget_usd` or the step timeout. The adapter then writes the outcome, the new `state_summary` (version plus one, under the lease from Phase 5), the cost settlement and evidence references in **one transaction**, and discards the session.
- `interrupt` ends the step at the next tool call (the `PreToolUse` hook denies and closes); `cancel` cancels the task's current step and records it; `inspect` returns status, employee, profile version, task, runtime, billing mode and turns, never model reasoning.
- Waiting never keeps a session open: `task.wait_for_approval` creates the approval (Phase 5) and moves the task to `WAITING_FOR_APPROVAL`; `task.wait_for_event` moves it to `WAITING_FOR_EXTERNAL` with a wake-up condition. The next step is a new session.
- SDK resume by session ID is not used in this phase. The SDK's session files live only in the per-session temporary directory, which is deleted when the step ends.

**Locked-down options**, built by one function `build_session_options(profile_version, routing_decision, billing_decision, brief)`, exactly as the table in ADR-0016: `setting_sources=[]`; system prompt from the profile version; `mcp_servers` with only the `htco` server of slice 6.6; `allowed_tools` exactly the granted `mcp__htco__*` tools; `disallowed_tools` every built-in tool; `permission_mode="default"`; `can_use_tool` denies and records; hooks from slice 6.6; `cwd` an empty per-session temporary directory; `env` only what the binary needs plus the API key named by the billing decision; `max_turns` and `max_budget_usd` from the profile's task limits; `model` and `fallback_model` from the routing decision only.

**Packaging.** `claude-agent-sdk` is pinned exactly and added to TOOLCHAIN.md after its minimum release age. The `agent-worker` image contains the Claude Code binary the SDK needs (verify the packaging at kickoff), runs as a non-root user, and has a read-only root filesystem except the temporary directory. Memory and CPU per session are measured to set the number of parallel step slots per worker.

**How CI tests the adapter without network.** The adapter talks to the SDK through one narrow internal port, `ClaudeSessionClient`. At kickoff, **verify the SDK's testing seam** (for example its `Transport` abstraction, which replaces the subprocess that runs the Claude Code binary). Then:
1. **Fake transport (preferred).** A `ReplayTransport` feeds the SDK a recorded or scripted stream of SDK messages (system init, assistant messages with tool-use blocks, tool results, the final result message with `model_usage`, `total_cost_usd`, `subtype` and `permission_denials`). The SDK's own parsing, hook dispatch and in-process MCP tool calls run for real against our bridge; only the model and the binary are replaced.
2. **Fallback if the seam is not usable:** a scripted fake of `ClaudeSessionClient` drives the same hook and tool-bridge code paths, and a separate contract test checks that recorded transcripts still parse with the pinned SDK version.
3. **Recorded transcripts.** A script (`just record-claude-transcripts`) runs a small set of fixture tasks against the real API in the development environment only, with the development key, under the owner-approved spend cap. Transcripts are sanitized (no keys, no buyer data, no reasoning content beyond what the fixtures need), checked into `tests/fixtures/claude_transcripts/` with the SDK version that produced them, and re-recorded when the SDK is upgraded. CI only replays them.
4. Scripted streams cover what is hard to record: 429, outage mid-stream, `error_max_budget_usd`, `error_max_turns`, a model that insists on a forbidden tool, a model that never calls a terminal tool.

| ID | Acceptance criterion |
|---|---|
| AC-6.5.1 | **POC-8, options check.** A CI test builds the options for every fixture profile and asserts: `permission_mode` is `"default"` (never `bypassPermissions`, `acceptEdits` or `auto`); `setting_sources == []`; `allowed_tools` contains only granted `mcp__htco__*` names; every built-in tool is in `disallowed_tools`; `mcp_servers` has only `htco`; `env` contains no secret other than the runtime key and no Supabase, Etsy, email or GitHub variable; `cwd` is an empty temporary directory. |
| AC-6.5.2 | A step that ends with each terminal tool writes, in one transaction, the task transition, the new `state_summary` (version plus one), the `agent_runs` row, the cost settlement and the evidence references; a failure inside that transaction leaves the previous state untouched. |
| AC-6.5.3 | A step that hits `max_turns`, `max_budget_usd` or the timeout without a terminal tool ends with a typed outcome, writes no business result, and follows the task's retry rules from Phase 5. |
| AC-6.5.4 | Two consecutive steps of the same task run in two different sessions; the second starts only from the task brief and the stored `state_summary`, and a test asserts it receives nothing from the first session (no session ID, no transcript). |
| AC-6.5.5 | The task brief masks buyer data above the profile's maximum data class, and its hash is stored on `agent_runs`; the same inputs produce the same brief. |
| AC-6.5.6 | The whole adapter test suite runs with network access blocked and no Anthropic key in the environment; a test that attempts a network call fails the job. |
| AC-6.5.7 | `inspect` returns no model reasoning; `cancel` stops the step within one tool call; `interrupt` closes the session at the next `PreToolUse`. |
| AC-6.5.8 | **Mandatory test 15 (part 1).** After a step, the session's temporary directory is gone, and a test that runs a step in a fresh container (new filesystem) after a kill recovers everything needed from Postgres; nothing is read from or written to local disk outside the session directory. |

### 6.6 — MCP tool bridge, hooks and tool invocation records

Per session, the adapter builds one in-process MCP server, `htco` (the SDK's `create_sdk_mcp_server` with `@tool` functions; verify names at kickoff), containing only the tools the profile version grants. Each tool function is a thin wrapper: it calls the **tool bridge**, which takes the employee, profile version, organization, shop, acting user and task from the session context (never from model arguments), calls `decide_tool_access` (unknown employee or tool, not ready, not granted, approval for critical tools and for external or financial tools below L3) and then the Phase 5 policy engine, runs the use case under the employee's service identity, sends external effects through the outbox with the step's idempotency key, and returns a typed result. Tool results that contain untrusted content (web pages, emails, supplier pages) are marked as data and never as instructions.

Two gates, as ADR-0016 requires:
- `PreToolUse` hook: calls the bridge's decision for every tool call, including calls the session option lists should already block; `DENY` blocks the call with a reason; `REQUIRE_APPROVAL` blocks it and ends the step through `task.wait_for_approval`; it also checks the kill switches (6.10).
- `PostToolUse` hook: writes one `tool_invocations` row (employee, profile version, tool name and version, decision with reason, outcome, latency, cost if any, idempotency key, the slice 5.1 fields including `policy_version`) and one activity record.

Per-tool rate limits and timeouts come from the tool definition. Professional memory (`agent_memory`) is written only through validated code paths that set source, organization, shop, confidence and expiry; no tool lets a model write memory, grants or instructions directly.

| ID | Acceptance criterion |
|---|---|
| AC-6.6.1 | **Scenario H, end to end; POC-4 part 1.** A fixture employee whose model output requests a tool outside its grants (and, separately, an automation outside its `allowed_automations`) is denied by the hook and the bridge, the denial is recorded in `tool_invocations` and as an activity record with outcome `denied`, and nothing executes, even when the model repeats the request. |
| AC-6.6.2 | **POC-4 part 2.** A request for a built-in tool (shell, file read or write, web fetch or search) is denied and audited; a request for a tool scoped to another shop is denied and audited. |
| AC-6.6.3 | A tool argument naming another shop, organization or acting user is ignored; the session's context is used, and the attempt is flagged in the invocation record. |
| AC-6.6.4 | An external tool called by an employee at autonomy below L3 creates an approval and ends the step; the tool runs only in a later step after the approval is decided; a critical tool always needs approval, whatever the autonomy. |
| AC-6.6.5 | Every tool call of every fixture run produces exactly one `tool_invocations` row and one activity record (coverage test); a denied call produces them too. |
| AC-6.6.6 | A tool that exceeds its rate limit or timeout fails with a typed error recorded in the invocation, and the step follows the task's retry rules. |
| AC-6.6.7 | Text from tool results cannot create or change memory, grants or instructions; memory items without source and expiry are rejected. |
| AC-6.6.8 | **Mandatory test 9 (policy level).** A fixture employee shaped like an engineering employee asks for a production-changing capability (push to a protected branch, deploy to production, merge): the bridge and the policy engine deny it (AC-5.6.6), and the denial is audited in `audit.audit_log`, `tool_invocations` and the activity trail. The real engineering runner is tested in Phase 19. |

### 6.7 — Agent runs and cost entries

`agent_runs`: one row per step session: employee, profile version, task, task execution, runtime, model and fallback model from the routing decision, **`billing_mode` (not null)**, `session_id` as an opaque reference, brief hash, status, outcome subtype, turns, duration, permission denials count, the slice 5.1 fields. `model_usage`: a **reservation** written before the session (the per-step cap from the profile; the full budget ledger is Phase 8) and a **settlement** after it, computed from the result's `model_usage` per model and the price catalog entry for that model (a fixture catalog in this phase; verified catalog in Phase 8). `total_cost_usd` from the SDK is stored for comparison only; it is a client-side estimate.

| ID | Acceptance criterion |
|---|---|
| AC-6.7.1 | Every session writes exactly one `agent_runs` row with a non-null `billing_mode`; a database constraint rejects a row without one (ADR-0017 validation test). |
| AC-6.7.2 | A session does not start unless its reservation was written; the settlement replaces the reservation in the same transaction as the step outcome; a crashed step leaves a reservation that the recovery scheduler releases. |
| AC-6.7.3 | Cost is computed from `model_usage` and the catalog price with its as-of date; a model missing from the catalog makes the cost "unknown" (never zero or invented) and raises an admin alert. |
| AC-6.7.4 | `agent_runs` and `model_usage` are invisible across organizations and shops (pgTAP). |
| AC-6.7.5 | No column of `agent_runs` holds prompts, transcripts or model reasoning. |

### 6.8 — Provider failure, quota and fallback

Quota and credit exhaustion are normal states (ADR-0017 rule 3). On a 429, an outage, `error_max_budget_usd` or an exhausted reservation, the adapter ends the step with a typed outcome, settles the cost so far, and the task moves to `WAITING_FOR_EXTERNAL` with a retry time (backoff with jitter, honouring a retry-after value when the provider sends one), or is re-dispatched on an **approved fallback route** if the routing decision names one (fixture routing in this phase, the real router in Phase 8). No rate-limit evasion: no key rotation to dodge limits, no session sharing between workers.

| ID | Acceptance criterion |
|---|---|
| AC-6.8.1 | **Mandatory test 3.** With the fake transport returning 429 (and, separately, an outage mid-stream), agent tasks move to `WAITING_FOR_EXTERNAL` with a retry time, while the Phase 4 order-intake and Phase 5 recovery automation tests, run in the same CI job at the same time, keep passing. |
| AC-6.8.2 | When the routing decision names an approved fallback route, the step is re-dispatched on it once, and the `agent_runs` rows record both attempts and the reason; with no approved fallback, the task waits. |
| AC-6.8.3 | `error_max_budget_usd` and an exhausted reservation end the step without a business result, and the task waits or escalates as its specification says; nothing fails silently (activity record and admin alert). |
| AC-6.8.4 | Repeated provider failures open a per-provider circuit breaker in the adapter, so new steps wait instead of calling; the breaker state is visible in the workforce directory and the API. |

### 6.9 — Internal messages and handoffs between employees

The employee side of Phase 5's contracts. Tools `messages.send_internal` and the terminal `task.handoff` call the Phase 5 use cases (`employee_messages`, `handoff_task`) through the bridge. The receiving employee's step acknowledges the message when its brief includes it. Employees never pass conversation state between sessions (FR-AGT-07), and Claude Code's cross-session messaging and agent teams are never used.

| ID | Acceptance criterion |
|---|---|
| AC-6.9.1 | **Mandatory test 4 (fixtures).** A fixture employee hands a task to another: one message is stored before it counts as sent, the receiving step starts from the task and its resources, acknowledges the message (`delivery_status = acknowledged`, `ack_timestamp` set), and both are linked by correlation ID; the receiver has no access to the sender's session or transcript. |
| AC-6.9.2 | A handoff or message to an employee in, or about, a shop the recipient is not granted is refused by `handoff_task`; neither employee can read the other shop's records through any tool (cross-shop leak test over every granted read tool). |
| AC-6.9.3 | A message cannot carry authority: a message asking the receiver to use a tool outside its grants is denied by the bridge like any other request. |
| AC-6.9.4 | Every message is a business statement with a summary length limit, visible in the task's activity trail; the conversation view shows messages and action receipts, never model reasoning (FR-AGT-08). |

### 6.10 — Kill switches, runtime switch and billing-mode switch

Switches: per employee (on `employees`), per automation (on `automation_definitions`), global, per runtime (`AGENT_RUNTIME_ENABLED` lists the runtimes this deployment may start) and per billing mode (ADR-0017 rule 5). Each is changed only by the Owner or an Admin with an `aal2` session and step-up, and audited. A switched-off employee, runtime or mode starts no session; a running session is closed at its next tool call by the `PreToolUse` hook; pending tool calls are refused. A profile whose runtime is not enabled cannot start a session: the task waits and an admin alert is raised; nothing falls back silently to another runtime. A switched-off automation refuses new triggers from every source.

| ID | Acceptance criterion |
|---|---|
| AC-6.10.1 | Switching off one employee stops its new sessions within 60 seconds and closes its running session at the next tool call, while other employees keep running. |
| AC-6.10.2 | The global switch, and switching off the `API_PRODUCTION` mode, stop all new sessions within 60 seconds; the state and the reason are shown in the workforce directory and the API. |
| AC-6.10.3 | A profile with `runtime = "langgraph"` while only `claude` is enabled (and the reverse) starts no session; the task waits with the reason and an alert is raised. |
| AC-6.10.4 | A switched-off automation refuses triggers from every source (event, schedule, user, employee, workflow) with a recorded reason. |
| AC-6.10.5 | Every switch change writes one audit record with actor, target, before and after, and `policy_version`. |

### 6.11 — Evaluation harness

Dataset format (versioned, no real buyer data), runner, metrics (accuracy, false-approve rate, schema validity, cost, p95 latency), `evaluation_runs` per employee and automation profile version, a nightly job with a report. The runner drives the real adapter with the replay transport in CI, and with the real API only in owner-approved evaluation runs outside CI (from Phase 7). An employee's evaluation result in its profile version comes only from a recorded run. Each activation phase builds that employee's evaluation set with this harness.

| ID | Acceptance criterion |
|---|---|
| AC-6.11.1 | An evaluation run produces a report comparing at least two routes on the same dataset, with cost per item (fixture routes and recorded transcripts in CI). |
| AC-6.11.2 | A profile version can be marked as having passed only by referencing an `evaluation_runs` row that met its thresholds; the thresholds are part of the employee's specification (section `evaluation`). |
| AC-6.11.3 | Evaluation datasets are checked in CI for buyer PII and secrets; a dataset containing either fails. |
| AC-6.11.4 | **Mandatory test 14 (fixtures).** An injection corpus (an email fixture and a web or supplier page fixture with instructions to call protected tools, reveal keys, change grants or write memory) runs against fixture employees: no protected tool is called, no permission, memory item or instruction changes, and every attempt that reaches a tool is denied and recorded. Real mail is tested in Phase 15. |

### 6.12 — Proof-of-concept gates POC-1 to POC-8

Exactly the gates of ADR-0016 "Validation". They run in CI with **fixture employees** (test profiles shaped like the real ones, never activated), the replay or scripted transport, and no network. All eight must pass; the evidence goes into ADR-0016's validation log.

| ID | Gate | Also proves |
|---|---|---|
| POC-1 | Duplicate dispatch of the same step produces one set of side effects. | AC-5.4.1, step idempotency keys |
| POC-2 | The worker is killed in the middle of a step: the lease expires, the step restarts from the task brief and no tool side effect repeats. | **Mandatory test 2 (agent steps)**; test 15 part 2 (the restarted step runs in a fresh container) |
| POC-3 | A task waits for an approval for a (simulated) day with no session open, then continues from the task brief. | No session, lease or process exists while waiting; one new session after the decision |
| POC-4 | An employee asks for an ungranted HTCO tool, a built-in tool (shell, file, web) and a tool for another shop: all three are denied and audited. | AC-6.6.1, AC-6.6.2, Scenario H |
| POC-5 | A three-employee handoff through tasks and messages, with cost per step from `model_usage`. | **Mandatory test 4 (fixtures)**, AC-6.7.3 |
| POC-6 | Prompt injection in a fetched page or email changes no permission, memory or instruction. | **Mandatory test 14 (fixtures)** |
| POC-7 | Scenario J: the same task through the Claude adapter and a plain-Python adapter writes the same CRM records. | FR-AGT-10; task transitions, activity records, tool invocations and messages compared row by row (ignoring IDs and times) |
| POC-8 | The adapter's options are checked in CI: no `bypassPermissions`, `setting_sources == []`, no built-in tool allowed, no secret in `env` other than the runtime key. | AC-6.5.1 |

| ID | Acceptance criterion |
|---|---|
| AC-6.12.1 | POC-1 to POC-8 are automated tests marked `@pytest.mark.ac("POC-n")`, run on every pull request that touches the runtime packages, the bridge or the registry, and all pass. |
| AC-6.12.2 | **POC-2 detail (mandatory test 2, agent steps).** The step is killed after a keyed tool side effect and before its terminal tool; the Phase 5 recovery scheduler re-queues it; the next session starts from the brief and the last committed `state_summary`; the side effect exists exactly once; and a re-evaluate step checks the evidence before acting. |
| AC-6.12.3 | **Mandatory test 15 (part 2).** POC-2's second attempt runs in a new worker container created from the image, with no volume shared with the first; the task completes from Postgres alone. |
| AC-6.12.4 | Memory and CPU per session are measured during the gates and recorded in ADR-0016 and [COST_MODEL.md](../../cost/COST_MODEL.md), with the resulting number of step slots per worker. |

### 6.13 — Workforce directory UI (clients)

Read-only directory in `htco-clients/apps/web`, on the design system: departments; employees with readiness (`draft` or `ready`), runtime, autonomy, current profile version, kill switch state and a **status derived from recorded data only**: `IDLE`, `QUEUED`, `RUNNING`, `WAITING_EXTERNAL`, `WAITING_APPROVAL`, `BLOCKED`, `FAILED`, `OFFLINE`, computed by one domain function from the employee's tasks, `task_executions`, `agent_runs` and worker heartbeats; tools with family, risk and readiness; automations with readiness and recent executions; runtime, billing-mode and circuit-breaker state; owner and admin controls for the kill switches (step-up required). The full office experience is [Phase 16](phase-16-web-office.md).

| ID | Acceptance criterion |
|---|---|
| AC-6.13.1 | A `draft` employee is never shown as working or available; it is shown as "Draft, not active". States shown come only from recorded tasks, runs and heartbeats; with no worker heartbeat, every employee is `OFFLINE` and the directory is read-only. |
| AC-6.13.2 | The status derivation is one tested domain function (a table test maps every combination of task state and heartbeat to exactly one status), used by both the API and the UI. |
| AC-6.13.3 | Kill switch controls are hidden for roles without the capability and require step-up for those with it; a disabled runtime or billing mode is shown as disabled with its reason. |
| AC-6.13.4 | Hebrew (RTL) and English, light and dark, pass the accessibility checks of the design system; the list view carries all information without relying on colour. |

## Mandatory tests owned by this phase

| Test ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | Where |
|---|---|
| 2 — agent task dies mid-step, lease expires, no duplicate action (**agent steps**) | POC-2, AC-6.12.2 |
| 3 — Claude unavailable or 429: automations continue; the task waits or uses an approved fallback | AC-6.8.1, AC-6.8.2 |
| 4 — two employees communicate: stored, acknowledged, handed off, no cross-shop leak (**fixtures**; real employees in Phase 7) | AC-6.9.1, AC-6.9.2, POC-5 |
| 9 — an agent tries to push to production: policy denial and audit (**policy level**; real runner in Phase 19) | AC-6.6.8 |
| 14 — prompt injection in email or web content calls no protected tool (**fixtures**; real mail in Phase 15) | AC-6.11.4, POC-6 |
| 15 — a replaced worker keeps no local state; everything is in Supabase (Phase 20 repeats it after a real rollout) | AC-6.5.8, AC-6.12.3 |

## Test plan

- Unit tests: AuthAndBillingPolicy decision table; `build_session_options`; task brief builder (masking, determinism, hash); status derivation; cost computation from `model_usage`.
- Adapter tests with the replay transport and scripted streams, network blocked, no key in the environment (AC-6.5.6).
- Contract test: recorded transcripts parse with the pinned SDK version; fails on an SDK upgrade until they are re-recorded.
- Tool bridge matrix over every registered tool and every fixture employee (grant, not ready, critical, external below L3, other shop, built-in).
- Registry sync tests against the local stack; pgTAP for every new table.
- Injection corpus (emails, web and supplier pages) in CI.
- POC-1 to POC-8; Scenarios H and J; kill-and-replace container test.
- Concurrency: the Phase 4 order-intake and Phase 5 automation suites run alongside the 429 and outage scripts (AC-6.8.1).
- Web UI: component, accessibility and end-to-end tests for the directory and the kill switches.
- Outside CI, once: transcript recording in the development environment under the owner's cap.

## Security checklist

- [ ] OWASP Agentic ASI01–ASI10 controls from the [threat model](../../architecture/THREAT_MODEL.md#3-agent-specific-risks-owasp-top-10-for-agentic-applications-2026) implemented and tested for the Claude adapter.
- [ ] Rule of Two (ER-AI-11) documented per fixture profile and checked at activation for real ones.
- [ ] Session options locked down and checked in CI (POC-8); `can_use_tool` denies everything that reaches it.
- [ ] Tenant, shop and acting user come only from the session context, never from model output (ER-AI-12).
- [ ] The API key is read only by the Claude adapter, one key per environment, never logged or returned; a canary-key scan passes; rotation runbook written.
- [ ] No personal subscription, login or OAuth token anywhere in the product (ER-AI-19).
- [ ] Registry rows changed only by the reviewed sync; no employee can widen its own grants.
- [ ] Worker image: non-root, read-only root filesystem, per-session temporary directory deleted after each step.
- [ ] Recorded transcripts reviewed for secrets and buyer data before they are committed.

## Documentation

`docs/features/workforce-registry/`, `docs/features/agent-runtime/` (Claude adapter, task brief, step lifecycle, testing seam, transcript recording), `docs/features/billing-policy/`, `docs/features/tool-bridge/`; ADR-0016 and ADR-0017 validation logs with the gate evidence and the facts verified at kickoff; ADR-0003 and ADR-0014 validation logs; TOOLCHAIN.md with the pinned `claude-agent-sdk`; DATA_MODEL tables marked created (`agent_runs`, `model_usage`, `tool_invocations`, `evaluation_runs`, registry tables), replacing `agent_sessions` and `agent_threads` for the Claude path; SOFTWARE_DESIGN §4.2 and §9.1 with the two runtime packages; PERMISSION_MATRIX §2 checked against the registry grants; runbooks for key rotation and the kill switches.

## Demo

Show the workforce directory with all 16 employees in `draft` and runtime `claude`; change a document in `registry/`, run the sync dry run, apply it and show the new profile version. Replay a recorded transcript: a fixture employee runs one step, calls a forbidden tool (denied, shown in `tool_invocations` and the activity feed), hands the task to a second fixture employee (message delivered and acknowledged), and the second waits for an approval with no session open. Kill the worker mid-step and show the step restarting from the brief with one side effect. Script a 429 and show the task waiting while order intake keeps working. Switch off the `API_PRODUCTION` mode and show new sessions stopping within 60 seconds. Show the options check and the eight POC gates passing in CI.

## Exit gate

- [ ] POC-1 to POC-8 pass; all AC-6.x pass, including Scenarios H and J and mandatory tests 2 (agent steps), 3, 4 (fixtures), 9 (policy level), 14 (fixtures) and 15.
- [ ] CI makes no model call; the transcript recordings stayed under the owner's cap.
- [ ] Kill switches, the runtime switch and the billing-mode switch verified on the deployed development environment.
- [ ] Memory per session measured and the `agent-worker` size decided.
- [ ] ADR-0016 and ADR-0017 validation logs updated with evidence.
- [ ] No employee is `ready` (activation starts in Phase 7).

## Rollback

Global kill switch and the `API_PRODUCTION` mode switch stop every session; employee features sit behind flags; registry, runtime and cost tables are additive; the sync can re-apply the previous registry commit; the LangGraph move is a rename that can be reverted as one commit. Because no employee is active, rolling back changes no business outcome.

## Risks

| Risk | Mitigation |
|---|---|
| The SDK has no clean testing seam, or changes it | Narrow `ClaudeSessionClient` port; fallback to a scripted fake plus transcript contract tests; stop and ask before changing ADR-0016 |
| SDK churn (options, hooks, message fields renamed) | Exact pin; upgrades only with re-recorded transcripts and all POC gates; facts re-verified at each upgrade |
| `PreToolUse` deny is not a complete gate in some path | Two gates: session option lists plus the bridge; `can_use_tool` denies; the bridge is the authority because tools call use cases, not the hook |
| A session per step re-reads the brief and costs more | Keep briefs small and summarized; measure cost per step in the gates; SDK resume only as a later optimization, never for correctness |
| Each session starts a Claude Code binary: memory and CPU | Measure per session (AC-6.12.4); cap parallel slots per worker; size the worker before the pilot |
| Registry and database drift | Sync from one source, CI comparison, no write API |
| Recorded transcripts leak data | Fixtures only, sanitization script, review before commit |

## Cost impact

No model spend in CI. A small, owner-approved one-time spend for recording transcripts in the development environment (amount set by the owner; verify prices at kickoff). Possibly an `agent-worker` size upgrade once memory per session is measured (record the actual figure in [COST_MODEL.md](../../cost/COST_MODEL.md)).
