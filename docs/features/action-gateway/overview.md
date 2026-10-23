# Action Gateway — Overview

| | |
|---|---|
| **Slug** | `action-gateway` |
| **Phase / slices** | Phase 5 — slice 5.12 (gateway and contracts); 5.13 (router and channels), 5.14 (approvals), 5.15 (limits) build on it |
| **Decision** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md); design [execution-engine.md](../../architecture/execution-engine.md) |
| **Status** | Built and tested in CI; not wired into the deployed API and worker until channels exist (5.13): they still hand handlers `UnavailableGateway` |
| **Last updated** | 2026-10-23 |

## What and why

Every business action of an automation or an AI employee goes through one gateway: it validates the action and its input, authorizes the actor, applies the approval rule, claims idempotency, routes to a channel, verifies the result and records it. One row per business effect in `app.action_executions`, an audit record for writes and personal data, and `actions.executed.v1` through the outbox.

## Acceptance criteria

| ID | Criterion | Status |
|---|---|---|
| AC-5.12.1 | The same `(action, idempotency_key)` submitted twice, including concurrently, executes once and returns the first result. | Pass (in memory and Postgres) |
| AC-5.12.2 | Unknown action or version, invalid input, a role without the capability, a non-granted shop (`not_found`) or an advisor: `DENIED` before any channel runs, with the reason recorded. | Pass |
| AC-5.12.3 | A W-policy action without a decided approval or the Owner's own trigger returns `PENDING_APPROVAL` and runs nothing; resubmitted with the approval, every check runs again. | Pass |
| AC-5.12.4 | Every result writes one `action_executions` row, an audit record for W-policies and personal data, and `actions.executed.v1` through the outbox; no secret in any of them. | Pass |
| AC-5.12.5 | Scenario H at the gateway: an automation outside its `allowed_actions` and an employee outside its grants are denied. | Pass |

## Scope

- **In:** `htco_application.execution` (`gateway.py` port and values, `contracts.py` definitions and catalog, `schema.py` JSON Schema subset, `service.py` the algorithm), `registry/actions/*.toml` (the first three contracts: `etsy.shop.read` R0, `etsy.receipts.read` R1, `etsy.inventory.update` W2), `packages/contracts/actions/*.json`, `allowed_actions` on automation definitions, `app.action_executions`, `PostgresActionStore`, the worker's `events.publish` deliverer, `just registry` checking the action contracts.
- **Out:** the Execution Router and real channels (5.13), the approval store (5.14 contract, Phase 7), breakers and limits (5.15), activity records (Phase 7).

## Known limits

- Routing takes the first binding whose channel is deployed and whose shop capability is present; 5.13 replaces it with the router.
- The W3 rule (Owner-only decisions) and step-up for W2 are enforced by the approval service (5.14, Phase 7), not yet here.
- An R1 read's output (buyer data) is returned once and never stored, so a replay returns the outcome without the output.
- Idempotency keys are unique per organization (the plan said per action); one tenant's key never meets another's.
