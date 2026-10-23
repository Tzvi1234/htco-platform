# Durable automation engine — Overview

| | |
|---|---|
| **Slug** | `automation-engine` |
| **Phase / slices** | Phase 5 — slice 5.10 (durable engine, definitions and executions tables, registry sync, API); slice 5.11 activates the first three automations on it |
| **Requirements** | [FR-AUT-01 to FR-AUT-03](../../product/PRD.md) |
| **Decision** | [ADR-0013](../../adr/0013-automation-engine.md) (engine), [ADR-0007](../../adr/0007-job-queue.md) (the queue underneath) |
| **Status** | Built and tested in CI; no automation is ready yet, so every trigger is refused until 5.11; user triggers are off by default (`HTCO_AUTOMATIONS_ENABLED`) |
| **Last updated** | 2026-10-22 |

## What and why

Every automation of the office runs through one entry point, `trigger`, whatever started it: an event, a schedule, a user, another workflow or (from Phase 8) an employee. The engine authorizes the trigger, stores one execution per `(automation_id, idempotency_key)` and puts it on the job queue; the worker runs it with leases, retries with backoff and jitter, and a dead-letter state. Definitions come from the reviewed `registry/automations/` files through a sync command, never from hand edits.

## Users and actors

- **System:** events and schedules trigger automations; the `agent-worker` runs executions (`automation.execute` jobs).
- **Owner, Admin, roles with an automation's capability:** start it by hand (`POST /v1/automations/{id}/executions`); Owner and Admin retry dead letters.
- **Office roles** (`view_agent_directory`): list automations and their executions.
- **Operator:** `htco registry sync --automations` (through the migration workflow).

## Acceptance criteria

| ID | Criterion (Given / When / Then) | Slice | Status |
|---|---|---|---|
| AC-5.10.1 | Given a trigger, when the same `(automation_id, idempotency_key)` is triggered again, also concurrently, then there is one execution row and the handler runs once; the second call returns the first execution. | 5.10 | Pass (in memory and Postgres) |
| AC-5.10.2 | Given an automation that is not ready, has no deployed handler, or is not callable by the trigger's kind, when it is triggered, then it is refused before any row is written and the refusal is logged with the reason. | 5.10 | Pass |
| AC-5.10.3 | Given a user, when their role lacks the automation's capability, then 403 (the policy engine decides); when they name a shop they may not use, then 404. | 5.10 | Pass (API and application) |
| AC-5.10.4 | Given a handler that keeps failing, when it runs, then it follows the definition's retry policy (attempts, backoff bounds) and ends `DEAD_LETTERED` with its last error; an admin retry is audited and reuses the same execution. | 5.10 | Pass |
| AC-5.10.5 | Given a worker killed during a `RUNNING` execution, when the lease expires, then exactly one `SUCCEEDED` and one business effect follow. | 5.10 | Pass in CI (real process killed) |
| AC-5.10.6 | Every state change is recorded with its actor and correlation ID; every transition outside the `ExecutionState` machine is rejected (domain and database). | 5.10 | Pass |
| AC-5.10.7 | `htco registry sync --automations` shows a diff in dry run, applies it idempotently with the registry commit, and CI fails when the database differs from `registry/automations/`. | 5.10 | Pass |

## Scope

- **In:** `app.automation_definitions`, `app.automation_executions`, `app.automation_execution_events`; `DurableAutomationEngine` with `PostgresExecutionStore`; the worker's `automation.execute` job; the registry sync command and its CI and migration-workflow steps; `GET /v1/automations`, `GET /v1/automations/{id}/executions`, `POST /v1/automations/{id}/executions` (with `Idempotency-Key`), `POST …/executions/{execution_id}/retry`; an optional `user_capability` per automation definition.
- **Out:** any production handler (5.11: Etsy Order Intake, Order Deduplication & Verification, Product Sales Counters), the `automation.trigger` tool for employees (Phase 8; the engine already checks grants), execution events on the outbox (`automations.execution_finished.v1` with the first producer), metrics and the queue health view (5.9), compensation.

## Known limits

- The engine reads definitions from the database on every trigger and run (cheap at pilot volume; cache when measured).
- Employees are not loaded into the engine's registry until Phase 8, so an employee's trigger is refused as unknown.
- A handler's timeout is not enforced beyond the job's lease; long handlers must stay below it.
- The first in-memory `AutomationEngine` was retired in slice 5.11 (audit P2-5): every test and deployed automation uses the durable engine, with `MemoryExecutionStore` in tests.
