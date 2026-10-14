# ADR-0003: LangGraph agent runtime behind a port

- **Status:** Accepted; to be validated by the Phase 8 proof-of-concept gates
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

No entries yet.
