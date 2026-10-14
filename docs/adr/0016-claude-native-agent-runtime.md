# ADR-0016: Claude-native agent runtime as the default adapter of the `AgentRuntime` port

- **Status:** Accepted; validated by the Phase 6 proof-of-concept gates. Amends [ADR-0003](0003-agent-runtime-langgraph.md): LangGraph stays as the alternative adapter and is not removed.
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Source:** HTCO OS Claude-Native Hybrid Edition, master plan v6.0 (kept outside the repository), sections 0–2, 11 and 13

## Context

The owner chose the Claude-Native Hybrid Edition of HTCO OS. It is the same business system as the LangGraph edition: the same 16 employees in seven departments, the same automations, tools, CRM, screens and approvals ([ADR-0012](0012-living-office-organization.md)). Only the agent runtime and the billing model change. The `AgentRuntime` port in `htco_application.agents.runtime` was written for this: Scenario J already proves that the business model does not import an agent framework.

Facts checked on 2026-10-14 against the official documentation ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview), [Python reference](https://code.claude.com/docs/en/agent-sdk/python)):

- The Python package is `claude-agent-sdk`. It runs the Claude Code binary as a library: `query()` for one session, `ClaudeSDKClient` for a session with several exchanges, `ClaudeAgentOptions` for configuration.
- Custom tools are in-process MCP tools (`@tool`, `create_sdk_mcp_server`) passed through `mcp_servers`. Built-in tools (files, shell, web) can be removed with `disallowed_tools`; a scoped deny rule holds in every permission mode.
- `PreToolUse` hooks can gate every tool call; `can_use_tool` only sees calls that fall through to a prompt, so it is not a complete gate on its own.
- `setting_sources=[]` stops the SDK from loading `.claude/` settings, skills and memory from the filesystem.
- `max_turns` and `max_budget_usd` bound a session (the budget is a client-side estimate). `ResultMessage` carries `session_id`, `subtype` (including `error_max_budget_usd`), `total_cost_usd`, `model_usage` and `permission_denials`.
- Sessions can be resumed or forked by ID, but they are stored on the local filesystem of the process. DigitalOcean App Platform replaces containers and wipes local files, so a session is never durable there.
- "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK." Hosted product runtime therefore uses API key authentication ([ADR-0017](0017-auth-and-billing-policy.md)).

## Decision drivers

- Same business contracts as the LangGraph edition: nothing above the port changes.
- Business identity and state live in Postgres, never in a session ([ADR-0012](0012-living-office-organization.md) rule 3).
- Deny by default: an employee can call only the HTCO tools its profile grants, through the tool bridge and the policy engine.
- Survive container replacement, crashes and duplicate jobs without repeating a side effect.
- Legal and terms-compliant authentication and billing.
- Keep the option to switch back to LangGraph, or to add another runtime, per employee.

## Options considered

1. **Claude Agent SDK in our `agent-worker`, behind the port (chosen).** Claude Code's agent loop, MCP tools, hooks, permissions and context management, in a process we operate. We must build durability ourselves, because sessions are local files.
2. **Claude Managed Agents** (Anthropic hosts the harness; sandbox hosted or self-hosted). Less to operate, but another set of contracts and data-residency questions. Revisited after the pilot if operating the SDK proves costly.
3. **Client SDK (Messages API) with our own tool loop.** Full control and simple durability, but we would rebuild context management and tool orchestration. Kept as the fallback adapter if option 1 fails a gate.
4. **LangGraph only** ([ADR-0003](0003-agent-runtime-langgraph.md)). Durable checkpoints, but the owner chose the Claude-native edition. It stays as the alternative adapter.

## Decision

Option 1. `ClaudeAgentRuntime` implements the existing `AgentRuntime` port (`invoke`, `resume`, `interrupt`, `cancel`, `inspect`) and becomes the default runtime. LangGraph stays as an alternative adapter.

### Packages

| Path (uv workspace member) | Python package | May import | Note |
|---|---|---|---|
| `packages/agent-runtime/claude/` | `htco_agent_runtime_claude` | application, domain, platform, `claude-agent-sdk` | New in Phase 6 |
| `packages/agent-runtime/langgraph/` | `htco_agent_runtime_langgraph` | application, domain, platform, LangGraph | Today's `packages/agent-runtime` (`htco_agent_runtime`), moved by a refactor-only slice in Phase 6; behaviour unchanged |

import-linter confines `claude_agent_sdk` to the first package and `langgraph` to the second, the same way provider SDKs are confined to `htco_model_router`. Neither adapter imports the other.

### Runtime selection

- Every employee profile version names its runtime (`runtime = "claude"` or `"langgraph"` in `registry/employees/*.toml`, a field added in Phase 6). The default is `claude`.
- A global setting, `AGENT_RUNTIME_ENABLED`, lists the runtimes this deployment may start. A profile whose runtime is not enabled cannot start a session: the task waits and an incident is raised. Nothing falls back silently.
- The model router ([ADR-0015](0015-decision-advisor-and-routing.md)) picks the model inside the chosen runtime. The Claude runtime runs Anthropic models only. GPT Images and Jev are reached as tools or specialists ([ADR-0018](0018-image-generation-specialist.md)), never as the session's main model.

### Sessions are disposable; task state is durable

- One session runs one bounded **step** of one business task for one employee profile version. It starts from a **task brief** built from Postgres: the task, its inputs, resource references, the summarized and versioned task state (`task_executions.state_summary`), the employee's instructions and the shop scope.
- A step ends when the employee calls a terminal HTCO tool (`task.complete`, `task.handoff`, `task.wait_for_approval`, `task.wait_for_event`, `task.escalate`), or when it reaches `max_turns`, `max_budget_usd` or its timeout. The adapter writes the outcome, the new state summary, costs and evidence references in one transaction, then the session is discarded.
- Waiting (for an approval, a supplier quote or a person) never keeps a session open. The task moves to `WAITING_FOR_APPROVAL` or `WAITING_FOR_EXTERNAL`; the event or a delayed job enqueues the next step, which starts a new session from the task brief. This is how `resume` is implemented.
- SDK `resume` by session ID may be used later as an optimization inside one worker's lifetime, but correctness never depends on it: crash recovery always restarts from the task brief.
- Steps are dispatched by the task dispatcher (Phase 5) with a lease and heartbeat. Everything before a terminal tool call is idempotent; side effects happen only through tools, under idempotency keys and the outbox.

### Locked-down session options

| Option | Value | Why |
|---|---|---|
| `setting_sources` | `[]` | No repository or user settings, skills, memory or hooks leak into an employee. Instructions come only from the versioned profile. |
| `system_prompt` | Built from the profile version: identity, mission, boundaries, skills, output schema | Prompts are code ([ER-AI-07](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)) |
| `mcp_servers` | One in-process server, `htco`, built per session from the profile's granted tools only | Unknown or ungranted tools do not exist in the session |
| `allowed_tools` | Exactly the granted `mcp__htco__*` tools | |
| `disallowed_tools` | Every built-in tool (shell, file read/write/edit, web fetch and search, subagents, notebooks) | Business employees reach files and the web only through HTCO tools that treat content as untrusted data |
| `permission_mode` | `"default"`, never `"bypassPermissions"`, `"acceptEdits"` or `"auto"` | CI test on the adapter's options |
| `can_use_tool` | Deny everything that reaches it, and record the denial | Nothing should fall through to a prompt in a headless worker |
| Hooks | `PreToolUse` calls the tool bridge: `decide_tool_access` plus the policy engine; deny, allow, or end the step with an approval request. `PostToolUse` writes the `tool_invocations` row and the activity record | Two gates: the server-side bridge and the session option lists |
| `cwd` | An empty per-session temporary directory, deleted afterwards | No repository or secret files are reachable |
| `env` | Only what the binary needs, plus the API key from the AuthAndBillingPolicy ([ADR-0017](0017-auth-and-billing-policy.md)). Never Supabase, Etsy, email or GitHub credentials | Least privilege ([ER-SEC-08](../engineering/ENGINEERING_RULES.md#7-security-er-sec)) |
| `max_turns`, `max_budget_usd` | From the profile's task limits; the budget ledger reserves the cost before the session starts | Budgets are enforced before the call ([ER-AI-06](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)) |
| `model`, `fallback_model` | From the routing decision only | |

The tool bridge, not the hook, is the authority. Tools call use cases under the employee's service identity, and the tenant and acting user come from the task, never from model arguments ([ER-AI-12](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)).

### Records

Each step writes `agent_runs` (employee, profile version, task, runtime, model, billing mode, `session_id` as an opaque reference, status, turns, duration), `model_usage` cost rows from the SDK's usage data (settling the reservation), `tool_invocations` and activity records. Model reasoning is not stored or shown ([ER-AI-09](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)); the step's output and evidence are.

### Engineering employees

Alex, Amir and Iris need code tools. They run in a separate, sandboxed engineering runner with built-in tools enabled inside an isolated Git worktree, with a GitHub token that can only push branches and open pull requests. They have no production credentials and cannot merge (Phase 19, [ER-AI-18](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)). This runner is never used for business employees.

### Cross-session messaging

Claude Code's cross-session messaging and agent teams are not used for business communication. Employees talk through `employee_messages` and task handoffs in Postgres (ACK, retries, attachments, permissions, audit). Development sessions may use these features; the product never depends on them.

## Consequences

- Positive: Claude Code's agent loop, context management and MCP tooling without a graph framework in the business path. Durability comes from our own task records, which already exist for the UI and supervision. The port and Scenario J keep LangGraph, or a Messages API adapter, one profile field away.
- Negative: we rebuild "interrupt and resume" as task steps with summarized state, so a long task pays for re-reading its brief. Each session starts a Claude Code binary, which costs CPU and memory and limits parallel slots per worker. The SDK changes often and needs pinned upgrades with gate tests.
- Follow-ups: Phase 6 adds the terminal task tools (`task.complete`, `task.handoff`, `task.wait_for_approval`, `task.wait_for_event`, `task.escalate`) to `registry/tools/`, and retires the `models.complete` grant for profiles on the Claude runtime (the session itself is the model call; other models are reached only as specialist tools). Phase 6 also adds the `runtime` profile field, moves LangGraph to `packages/agent-runtime/langgraph/`, builds the adapter, the MCP tool bridge and the hooks, and runs the gates. Pin `claude-agent-sdk` exactly and add it to [TOOLCHAIN.md](../engineering/TOOLCHAIN.md) after the minimum release age. Measure memory per session to size the worker.

## Validation

The Phase 6 proof-of-concept gates ([phase file](../plan/phases/phase-06-workforce-claude-runtime.md)):

1. Duplicate dispatch of the same step produces one set of side effects.
2. The worker is killed in the middle of a step: the lease expires, the step restarts from the task brief and no tool side effect repeats.
3. A task waits for an approval for a (simulated) day with no session open, then continues from the task brief.
4. An employee asks for an ungranted HTCO tool, a built-in tool (shell, file, web) and a tool for another shop: all three are denied and audited.
5. A three-employee handoff through tasks and messages, with cost per step from `model_usage`.
6. Prompt injection in a fetched page or email changes no permission, memory or instruction.
7. Scenario J: the same task through the Claude adapter and a plain-Python adapter writes the same CRM records.
8. The adapter's options are checked in CI: no `bypassPermissions`, `setting_sources == []`, no built-in tool allowed, no secret in `env` other than the runtime key.

## Validation log

- 2026-10-14 — Documentation facts above verified against code.claude.com. No adapter code exists yet.
