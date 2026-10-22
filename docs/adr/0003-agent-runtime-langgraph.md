# ADR-0003: LangGraph agent runtime behind a port

- **Status:** Accepted; to be validated by the Phase 8 proof-of-concept gates. Scope narrowed on 2026-10-14 by [ADR-0012](0012-living-office-organization.md) and [ADR-0013](0013-automation-engine.md): LangGraph orchestrates employees' reasoning only; deterministic workflows run in the automation engine, and the organization's model never imports it. Amended 2026-10-23 by [ADR-0019](0019-unified-execution-engine-and-action-gateway.md): sessions act only through the Action Gateway; the LangGraph Postgres checkpointer serves employees' sessions only.
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

Agents must run durable, multi-step workflows that can wait for days (supplier quotes), pause for human approval, survive crashes and restarts, and never repeat side effects. The original planning brief selects LangGraph for this track and requires that business code never depends on the agent framework.

## Decision drivers

- Durable execution with checkpoints in our own Postgres.
- First-class pause and resume for approvals and long waits.
- Provider independence for models.
- No lock-in: replacing the runtime must not touch domain or persistence code.
- Security history and maintenance of the dependency chain.

## Options considered

1. **LangGraph (open source) with Postgres checkpoints.** Mature durable execution and interrupts; MIT licensed; we operate it ourselves.
2. **Claude Agent SDK or managed agent platforms.** Fast to start, but less control over checkpoints and multi-provider routing, and different cost model.
3. **Our own state machine runner.** Full control, but we would rebuild checkpointing, interrupts and graph tooling.

## Decision

Option 1, behind an `AgentRuntime` port in `htco_application`, implemented in `htco_agent_runtime`:

- LangGraph 1.2.x, `langgraph-checkpoint` ≥ 4.3 (patched deserialization issues), `langgraph-checkpoint-postgres` 3.1.x with `AsyncPostgresSaver`.
- Checkpoint tables live in the `agent_runtime` schema, created by our migrations, reached over a direct or session-mode connection (checkpoints need prepared statements and pipelining).
- `LANGGRAPH_STRICT_MSGPACK=true` and an encrypted serializer are mandatory.
- We do **not** use the `langchain` package at first; nodes call our own ports (model gateway, use cases). `langchain-core` comes in as a LangGraph dependency only.
- One run per thread at a time, enforced by our own row lock (open-source LangGraph does not prevent concurrent runs on a thread).
- Graph versions are stamped on threads; nodes are renamed by add-then-remove; old checkpoints become test fixtures.

## Consequences

- Positive: durable waits and approvals without polling loops; full control of data and costs; the port keeps the option to swap runtimes.
- Negative: we operate workers, checkpoints and upgrades ourselves; LangGraph releases are frequent and need upgrade testing.
- Follow-ups: Phase 1 runs a LangGraph smoke test on our Python version; Phase 8 runs the six proof-of-concept gates.

## Validation

Six gates (defined in Phase 8) must pass before any agent gains autonomy: a duplicate `order.paid` handled once; an approved external write delivered exactly once despite a worker crash; an interrupt resumed a day later; unauthorized tools denied; a three-agent handoff with cost analysis; prompt injection in a supplier page has no effect on tools.

## Validation log

- 2026-10-14 — LangGraph 1.2.14 imports and runs on CPython 3.14.8 (`tests/agent_runtime`). The `AgentRuntime` port now has `invoke`, `resume`, `interrupt`, `cancel` and `inspect` (`htco_application.agents.runtime`), proven framework-neutral by Scenario J.
- 2026-10-23 — ADR-0019: every tool call of a session goes through the tool bridge into the Action Gateway; checkpoints (`AsyncPostgresSaver`, `agent_runtime` schema) serve sessions only and never hold business state. A session waiting on a Human-channel task or an approval is interrupted and resumed from its checkpoint.
