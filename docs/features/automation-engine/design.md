# Durable automation engine — Design

## Components and layers

| Layer | Module | Responsibility |
|---|---|---|
| Domain | `htco_domain.automation` | `AutomationDefinition` (now with optional `user_capability`), `AutomationExecution` (tenant, causation, last error), the `ExecutionState` machine (admin retry: `DEAD_LETTERED → RETRY_SCHEDULED`), `RetryPolicy.jittered_backoff` |
| Application | `htco_application.automation.engine` | `authorize`: the one gate (readiness, deployed handler, trigger kind, employee grants), and `TriggerRequest`; the first in-memory `AutomationEngine` was retired in slice 5.11 (audit P2-5) |
| Application | `htco_application.automation.durable` | `DurableAutomationEngine` (`trigger`, `execute`, `retry_dead_lettered`, `resume`, `cancel_waiting`), the `DurableExecutionStore` port, `StateChange`, `HandlerContext`, `Completed` and `Waiting` |
| Application | `htco_application.execution.gateway` | The `ActionGateway` port with its request and result values (ADR-0019); `UnavailableGateway` refuses every action until the gateway exists (5.12) |
| Application | `htco_application.automation.records` | Definition rows, document hashes, the sync plan, `registry_from_rows`, execution summaries |
| Application | `htco_application.automation.requests` | User actions: list, executions, trigger, admin retry, each decided by the policy engine |
| Adapters | `htco_persistence.automation` | `PostgresExecutionStore`, `PostgresAutomationCatalog`, `PostgresExecutionReader` |
| Transport | `htco_api.automations`, `htco_api.cli` (`htco registry sync`), `htco_worker.main` (`automation.execute`) | Routes, the sync command, the worker's job |

## Contracts

- **Tables** (`supabase/migrations/20261023140000_automation_engine.sql`):
  - `app.automation_definitions` — primary key `(id, version)`; the columns of `AutomationDefinition`, `specification` (18 sections), `document` (the whole reviewed TOML document), `specification_hash` (SHA-256 of the document), `registry_commit`, `synced_at`. The current definition is the highest version.
  - `app.automation_executions` — unique `(automation_id, idempotency_key)`; `organization_id`, `shop_id`, `(automation_id, automation_version)` referencing a stored definition, trigger, actor, state, attempts, correlation and causation IDs, last error, payload (≤ 16 KB). A trigger refuses any state change outside the machine (`23514`).
  - `app.automation_execution_events` — one row per state change: from, to, actor, correlation ID, reason, time; append-only.
- **Endpoints:** `GET /v1/automations`, `GET /v1/automations/{automation_id}/executions` (cursor), `POST /v1/automations/{automation_id}/executions` (`Idempotency-Key` required; 202), `POST /v1/automations/{automation_id}/executions/{execution_id}/retry` (202). See `packages/contracts/openapi.yaml`.
- **Jobs:** `automation.execute` (`execution:<id>:<attempt>`), carrying the execution's organization.
- **Command:** `htco registry sync --automations [--root] [--commit] [--apply | --check]`.
- **Setting:** `HTCO_AUTOMATIONS_ENABLED` (API, default off).

## Flow

```mermaid
sequenceDiagram
  participant T as Trigger (event, schedule, user)
  participant E as Engine (API or worker)
  participant DB as Postgres
  participant W as Worker
  T->>E: trigger(request)
  E->>E: authorize (refusal logged with its reason, nothing written)
  E->>DB: execution + PENDING event + automation.execute job (one transaction; a replay returns the first)
  W->>DB: claim the job (lease)
  W->>E: execute(execution_id)
  E->>DB: RUNNING (only if still PENDING or RETRY_SCHEDULED)
  E->>E: handler(payload)
  alt success
    E->>DB: SUCCEEDED
  else failure
    E->>DB: FAILED → RETRY_SCHEDULED + next job at backoff with jitter, or FAILED → DEAD_LETTERED
  end
```

## Handlers

A handler is `async def handler(context: HandlerContext) -> Completed | Waiting`. The context holds the execution and automation IDs, the triggering actor, the organization and shop, the correlation and causation IDs, the attempt, the payload and an `ActionGateway` client. Business effects go through `context.gateway.submit(ActionRequest(...))`, never straight to use cases or the outbox (ADR-0019). An action that returns `pending_approval` or `handed_to_human` makes the handler return `Waiting(reason)`: the execution moves to `WAITING` and its job ends, so no lease is held while a person decides. `resume` (an approval decided, a task done; wired to `approvals.decided.v1` in 5.14 and Phase 7) moves it to `RETRY_SCHEDULED` with a new job, without using a retry attempt; `cancel_waiting` (rejected, expired) ends it `CANCELLED`.

## State machine

`PENDING → RUNNING → SUCCEEDED`; `RUNNING → WAITING → RETRY_SCHEDULED | CANCELLED`; `RUNNING → FAILED → RETRY_SCHEDULED → RUNNING`; `FAILED → DEAD_LETTERED`; `DEAD_LETTERED → RETRY_SCHEDULED` (an admin, audited); `FAILED → COMPENSATED`; `PENDING/RETRY_SCHEDULED → CANCELLED`. `FAILED` is never at rest: a failure is stored together with its next state. An execution found `RUNNING` when its job runs again belongs to a worker that died; that run is recorded as a failed attempt (`LeaseExpired`) and the execution continues (retry or dead letter).

## Decisions and alternatives

| Decision | Alternatives considered | Why |
|---|---|---|
| A new `DurableAutomationEngine` sharing `authorize`; the first in-memory engine was later retired (5.11, audit P2-5) | Rewriting `AutomationEngine` | Its tests now run against the durable engine with `MemoryExecutionStore` |
| One job per attempt; handler failures follow the definition's policy, crashes the job's | One job retried by the queue | The definition owns attempts and backoff bounds (AC-5.10.4); a crash still counts as an attempt |
| Optimistic `advance` on the stored state and attempts | Row locks held during the handler | No transaction stays open while a handler runs; two runs cannot both move an execution |
| Definitions from the database at run time | The registry files in the image | Rollback is a sync (`readiness` back to `draft`), and the image does not carry `registry/` |
| `user_capability` per definition, absent means no user may trigger | A fixed capability for all automations | Deny by default; each automation names the permission matrix row that guards it |
| Execution keys of users are `user:<organization>:<Idempotency-Key>` | The header alone | One tenant's key can never meet another tenant's execution |
| A removed automation blocks the sync | Deleting or retiring automatically | Rows are history; retiring is a reviewed change (`readiness = "retired"`) |
