# ADR-0013: An automation registry and workflow engine independent of the agent runtime

- **Status:** Accepted; amended 2026-10-23 by [ADR-0019](0019-unified-execution-engine-and-action-gateway.md) (handlers act through the Action Gateway and its channels)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Related:** [ADR-0012](0012-living-office-organization.md), [ADR-0007](0007-job-queue.md) (job queue), [ADR-0003](0003-agent-runtime-langgraph.md)

## Context

Most of HTCO's work is repeatable: ingest an order, update counters, sync tracking, record a ledger entry, reconcile, follow up on a quote, send a report. The first plan ran this in "workers" without a common model, and some of it (publishing, inspection) was assigned to agents. The same action must be callable from an external event, a schedule, an authorized agent, a user, or another workflow, always through the same authorization and idempotency rules, and without spending model tokens.

## Decision drivers

- Exactly-once business effect under at-least-once delivery.
- One entry point with authorization for every trigger kind.
- No dependency on LangGraph: deterministic work must not wait for or pay for a reasoning runtime.
- Each workflow specified, owned, tested and measured like a product.

## Options considered

1. **LangGraph graphs for everything.** One tool, but deterministic actions would carry runtime overhead and checkpoints, and couple business operations to the agent framework.
2. **An external workflow engine (Temporal, Inngest, a hosted service).** Strong durability, but another platform to run or pay for at pilot scale.
3. **Our own small engine on the Postgres job queue**, with automations as versioned definitions in a registry.

## Decision

Option 3.

- **Definitions** (`AutomationDefinition`): ID, name, version, business owner, primary trigger, `callable_from`, retry policy, timeout, side-effect class, approval requirement, `ai_steps` (empty unless a named step needs reasoning), readiness, activation phase, and an 18-section specification (identity, owner, trigger, input and output schemas, preconditions, steps, dependencies, error handling, retries, idempotency, timeouts and SLA, permissions, audit, tests, recovery, approvals, metrics). Stored in `registry/automations/` and, from Phase 8, in the database with versions.
- **Executions** (`AutomationExecution`): an explicit state machine `PENDING → RUNNING → SUCCEEDED`, with `FAILED → RETRY_SCHEDULED → RUNNING`, `DEAD_LETTERED`, `COMPENSATED` and `CANCELLED`; unique per `(automation_id, idempotency_key)`.
- **Engine** (`AutomationEngine`): one `trigger` entry point that checks readiness, the deployed handler, the trigger kind and, for employees, that the trigger is `agent_tool` and the automation is in the employee's grants. A replay returns the existing execution. Handlers call use cases; external effects go through the outbox.
- **Durability**: Phase 5 puts executions on the job queue (leases, heartbeats, backoff with jitter, dead letters) with inbox and outbox. Until then the engine runs in-process against an `ExecutionStore` port.
- **The 15 initial workflows** are in `registry/automations/`: Etsy Order Intake; Order Deduplication & Verification; Product Sales Counters; Catalog Draft Creation & Publishing; Supplier Quote Request & Follow-up; Tracking Synchronization; Finance Ledger Ingestion; Inventory & Supplier Price Monitoring; Order & Financial Reconciliation; Incident Detection & Escalation; Email Intake & Routing; Scheduled Reporting & Notifications; Agent Task Scheduling & Recovery; Quality Review Scheduling; Optimization Review Scheduling.
- **Deployment**: automations run in `agent-worker` (renamed from `worker`) until isolation or load justifies a separate `automation-worker`.

## Consequences

- Positive: deterministic work is cheap, testable and runtime-independent; every trigger is authorized the same way; replay safety is structural.
- Negative: we own the engine's durability features; workflows with long waits need careful state design (they use task states such as `WAITING_FOR_QUOTES`, not open runtime sessions).
- Follow-ups: job-queue spike and durable store (Phase 5); per-workflow schemas in `packages/contracts/automations/`; the Automation Center in the office UI (Phase 15).

## Validation

Scenario G (a paid order replayed many times yields one order and correct counters) passes in memory now and is repeated against Postgres in Phase 5; Scenario B (a failed CRM write stays recoverable and is verified) in Phase 5 and Phase 10.

## Validation log

- 2026-10-14 — In-memory engine: replays, retries, dead letters and employee authorization tested (`tests/application/test_automation_engine.py`).
- 2026-10-22 — Durable engine on Postgres (slice 5.10, `htco_application.automation.durable`, `htco_persistence.automation`): one execution per key under concurrent triggers, refusals before any write, the definition's retry policy into `DEAD_LETTERED`, audited admin retry on the same execution, a worker killed during a run leading to exactly one success and one effect, every state change recorded and the machine enforced in the database too, definitions loaded only by `htco registry sync --automations` (CI checks the database matches the registry). Tests run against Postgres 17 in CI (`tests/integration/test_durable_engine.py`, `tests/integration/test_worker_process.py`, pgTAP `12`). Scenario G and Scenario B part 1 against Postgres arrive with the first handlers in slice 5.11. Aligned with ADR-0019 (2026-10-23): handlers get a `HandlerContext` with an `ActionGateway` client, and `WAITING` (domain, database check and transition trigger) lets a run wait for an approval or a person without a lease.
- 2026-10-23 — Amended by ADR-0019: an automation's handler performs its business actions through the Action Gateway (permissions, approvals, idempotency, limits, verification, audit) and the Execution Router (API, Browser, Internal, Human). `ai_steps` stays empty; an automation reaches the Browser channel only through deterministic Playwright recipes. The engine itself (PR #58) is kept.
