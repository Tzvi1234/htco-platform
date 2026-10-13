# Phase 8 — Agent runtime and model gateway

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (agent directory, budgets) |
| **Size** | L |
| **Depends on** | Phase 7 |
| **Requirements** | FR-ADM-03, FR-AGT-01, FR-AGT-02, FR-AGT-03, FR-AGT-04, FR-AGT-05, FR-AGT-06, FR-AGT-07, FR-IAM-07, FR-INC-04, FR-MDL-01, FR-MDL-02, FR-MDL-03, FR-MDL-04, FR-MDL-05, FR-MDL-06, FR-MDL-07 |
| **Status** | Not started |

## Progress

- [ ] 8.1 Model gateway
- [ ] 8.2 Agent runtime
- [ ] 8.3 Tool registry and bridge
- [ ] 8.4 Agent profiles and versions
- [ ] 8.5 Proof-of-concept gates (from the original brief)
- [ ] 8.6 Eval harness
- [ ] 8.7 Debugger v0
- [ ] 8.8 Agent directory and budgets UI

## Goal

Agents can run safely: durable LangGraph workflows that pause for approvals and long waits, typed tools behind the policy engine, every model call routed, budgeted and logged, and the six proof-of-concept gates from the original brief passing as automated tests. No agent gets autonomy before this phase's gate.

## Scope

**In:** model gateway (router, catalog, provider adapters, budget ledger, logging, circuit breakers, kill switches), agent runtime (LangGraph adapter, checkpoints, thread registry, resume through jobs, graph versioning), tool registry and bridge, agent profiles and versions, POC gates, eval harness skeleton, Debugger v0 on incidents, agent directory and budget settings UI.

**Out:** Listing Health (Phase 9) and other workflows.

## Owner prerequisites

- Anthropic and OpenAI API keys created for HTCO (separate projects or workspaces with spend limits at the provider too).
- Monthly AI budget cap (D-06); D-03 decided (first workflow).

## Slices

### 8.1 — Model gateway

`ModelGateway` port; deterministic router with versioned policy (`NO_AI` first, then tier and fallback chain); `model_catalog` seeded with verified model IDs and prices (as-of date) by pull request; Anthropic and OpenAI adapters using structured outputs; `model_usage` and `routing_decisions`; budget ledger with reserve and settle at all levels, thresholds at 70/90/100% and a safety reserve; circuit breaker and kill switch per provider; PII masking step.

| ID | Acceptance criterion |
|---|---|
| AC-8.1.1 | A call whose reservation would exceed any cap is refused before any provider request. |
| AC-8.1.2 | When the primary provider fails (fake adapter), the fallback is used and both attempts are logged. |
| AC-8.1.3 | Every call writes one routing decision and one usage row with tokens, cache tokens and cost. |
| AC-8.1.4 | An architecture test fails if any package other than `htco_model_router` imports a provider SDK. |
| AC-8.1.5 | Structured output that fails schema validation is treated as an error, never passed on. |
| AC-8.1.6 | Buyer names, emails and addresses are masked before a prompt leaves the gateway unless the task's data class allows them; fixtures prove it, and every unmasked call is recorded. |

### 8.2 — Agent runtime

`AgentRuntime` port and LangGraph adapter; `agent_runtime` schema migration for checkpoints and store; strict msgpack and encrypted serializer; `agent_threads` with lock, `graph_version` and status; runs started and resumed by jobs; durability `sync` for long-wait graphs; recursion limit 25–50; checkpoint retention job.

| ID | Acceptance criterion |
|---|---|
| AC-8.2.1 | A run interrupted for approval resumes after a worker restart from its checkpoint. |
| AC-8.2.2 | Two resume jobs for the same thread never run concurrently; the second is a no-op. |
| AC-8.2.3 | A fixture checkpoint from the previous graph version resumes correctly after a compatible change. |

### 8.3 — Tool registry and bridge

Typed tools with input and output schemas, timeouts, retry limits, side-effect levels and required autonomy; the bridge calls the policy engine with the run's identity, tenant and acting user (never from model arguments); external effects go through the outbox; `tool_usage` and `agents.tool_denied.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-8.3.1 | A model output requesting a tool outside the profile's allowlist is denied, audited and does not execute. |
| AC-8.3.2 | A tool argument naming another shop is ignored; the run's shop is used, and the attempt is flagged. |
| AC-8.3.3 | Agent memory is written only through validated code paths that set source, organization, shop, confidence and expiry; text from tool results or web pages cannot create or change memory directly. |

### 8.4 — Agent profiles and versions

`agent_profiles`, `agent_versions` (prompt files, tools, autonomy level, budget), seeded for Quality Supervisor, Debugger and General Manager; every run records its profile version.

| ID | Acceptance criterion |
|---|---|
| AC-8.4.1 | Every run stores the agent profile version and the hash of each prompt file it used; a CI check fails when a prompt file changes without a new profile version. |

### 8.5 — Proof-of-concept gates (from the original brief)

| ID | Gate |
|---|---|
| POC-1 | A duplicated `order.paid` event produces one order and one downstream task. |
| POC-2 | An approved external write (for example a listing update) is delivered exactly once when the worker is killed between the database commit and the delivery, and the pre-write re-read prevents a duplicate when the write had already landed. |
| POC-3 | A workflow waits for a supplier quote (interrupt), and resumes a (simulated) day later from a delayed job. |
| POC-4 | Manager and Debugger cannot execute a tool outside their allowlist, even when the model insists. |
| POC-5 | A research task hands off between three agents through structured events, with cost reported per step. |
| POC-6 | A prompt-injection payload in a supplier page fixture changes no tool permission and causes no side effect. |

Each gate is an automated test in CI; all six must pass.

### 8.6 — Eval harness

Dataset format (versioned, no real buyer data), runner, metrics (accuracy, false-approve rate, cost, p95 latency), nightly job with a report; eval results feed the model catalog's quality scores.

| ID | Acceptance criterion |
|---|---|
| AC-8.6.1 | An eval run produces a report comparing at least two routes on the same dataset with cost per item. |

### 8.7 — Debugger v0

Graph that reads an incident's evidence, proposes an allowlisted remediation (for example re-running a sync) or escalates with a summary; non-allowlisted fixes create approvals.

| ID | Acceptance criterion |
|---|---|
| AC-8.7.1 | The Debugger can trigger only allowlisted remediations; everything else becomes an approval or a developer task. |
| AC-8.7.2 | Incidents resolve only on evidence, never on the Debugger's claim. |

### 8.8 — Agent directory and budgets UI

Read-only agent directory (profiles, versions, autonomy, recent runs, costs); owner budget settings with thresholds; kill switches.

| ID | Acceptance criterion |
|---|---|
| AC-8.8.1 | Activating the global kill switch stops new runs within 60 seconds and shows the state everywhere. |

## Test plan

Fake model gateway for all unit and graph tests; injection corpus; POC gate tests; eval harness nightly; one manual live smoke test per provider with a tiny budget.

## Security checklist

- [ ] OWASP Agentic ASI01–ASI10 controls from the [threat model](../../architecture/THREAT_MODEL.md#3-agent-specific-risks-owasp-top-10-for-agentic-applications-2026) implemented and tested.
- [ ] Rule of Two applied to every graph (documented per graph).
- [ ] Provider keys only in the gateway's environment; no key in prompts, logs or checkpoints.
- [ ] Checkpoints encrypted; strict msgpack on.

## Documentation

`docs/features/model-gateway/`, `docs/features/agent-runtime/`, `docs/features/tool-registry/`; ADR-0003 validation recorded; COST_MODEL updated with real per-call costs.

## Demo

Show a Debugger run on a real incident with its receipts; trigger a budget cap; trigger a denied tool; kill the worker during an interrupt and resume.

## Exit gate

- [ ] POC-1 to POC-6 pass; all AC-8.x pass; budget and kill switch verified in production.

## Rollback

Global kill switch; agent features behind flags; runtime tables are additive.

## Risks

| Risk | Mitigation |
|---|---|
| LangGraph upgrade churn | Pin; upgrade deliberately with checkpoint-compatibility tests |
| Cost surprises | Reserve-before-call, low caps during the pilot, Batch API for bulk work |

## Cost impact

AI usage within the owner's cap; possibly worker memory upgrade (about $12/month).
