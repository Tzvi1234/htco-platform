# Action Gateway — Overview

| | |
|---|---|
| **Slug** | `action-gateway` |
| **Phase / slices** | Phase 5 — slices 5.12 (gateway and contracts), 5.15 (breakers, limits, retries) and 5.13 (router and channels); 5.14 (approvals) builds on them |
| **Decision** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md); design [execution-engine.md](../../architecture/execution-engine.md) |
| **Status** | Built and tested in CI. Not yet wired into the deployed API and worker: they still hand handlers `UnavailableGateway`. The wiring (Postgres principals, shops, approvals, Etsy shop IDs) comes with 5.14 and the first ready automation (5.11). |
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
| AC-5.13.1 | A contract test runs one fixture action through the API, Internal and fake Browser adapters and gets the same `ActionResult` shape, audit and activity output. | Pass (the event stands for the activity record until Phase 7) |
| AC-5.13.2 | An automation can never be routed to a non-deterministic binding (Browser Use); the router refuses with `scope_missing` or the next deterministic binding. | Pass |
| AC-5.13.3 | A browser binding is never chosen when the environment is not `READY`, the action's flag is off, or its Etsy scope row is missing or older than the flag. | Pass |
| AC-5.13.4 | The fake Browser adapter's challenge outcome stops the action, returns `HANDED_TO_HUMAN`, creates one Human task and retries nothing. | Pass (in memory and Postgres) |
| AC-5.13.5 | An API refusal for a missing scope or capability never falls through to a browser binding unless that binding has its own recorded scope. | Pass |
| AC-5.15.1 | With two workers and four shops, total Etsy calls never exceed the configured QPS in any second, or the QPD in a day (simulated clock). | Pass (in memory and Postgres) |
| AC-5.15.2 | Five failures in 60 s open the breaker for that key; calls return `DEFERRED` with `retry_at`; half-open after the cooldown lets one call through. | Pass (in memory and Postgres) |
| AC-5.15.3 | A 4xx other than 429 is never retried; a 429 waits for `Retry-After`. | Pass |
| AC-5.15.4 | Two writes to the same shop never run at the same time when the action has a per-shop write slot. | Pass (in memory and Postgres) |

## Scope

- **In:** `htco_application.execution` (`gateway.py` port and values, `contracts.py` definitions and catalog, `schema.py` JSON Schema subset, `service.py` the algorithm), `registry/actions/*.toml` (the first three contracts: `etsy.shop.read` R0, `etsy.receipts.read` R1, `etsy.inventory.update` W2), `packages/contracts/actions/*.json`, `allowed_actions` on automation definitions, `app.action_executions`, `PostgresActionStore`, the worker's `events.publish` deliverer, `just registry` checking the action contracts.
- **In (5.15):** `htco_domain.guards` (breaker rules, windows), `htco_application.execution.guards` (ports, `InlineRetry`, in-memory guards), `htco_persistence.guards` (Postgres), `breaker_key` on bindings, `[[rate_limits]]` budgets, `retry_at` on results and rows, `DEFERRED` resumable.
- **In (5.13):** `htco_application.execution.router` (`ExecutionRouter`, the `BrowserGate` port, `NoBrowser`), `htco_application.execution.channels` (`EtsyApiChannel` for reads through `MarketplacePort`, writes refused before sending; `InternalChannel`; the `BrowserChannel` Protocol and `FakeBrowserChannel`; `HumanChannel` and `MemoryHumanTasks`), `htco_domain.browser.EnvironmentStatus`, hand-off and resume in the gateway, and the catalog's browser-binding rules.
- **Out:** the approval store (5.14 contract, Phase 7), activity records (Phase 7), the environment registry and its table (5.16), the real browser controller (8A), Etsy write delivery through the outbox (Phases 6 and 7), and wiring into the deployed processes.

## Known limits

- Until the environment registry (5.16) exists, the deployed default `NoBrowser` skips every browser binding.
- Human tasks are in memory (`MemoryHumanTasks`) until Phase 7's task store; a process restart loses an open hand-off (nothing is deployed yet).
- The W3 rule (Owner-only decisions) and step-up for W2 are enforced by the approval service (5.14, Phase 7), not yet here.
- An R1 read's output (buyer data) is returned once and never stored, so a replay returns the outcome without the output.
- Idempotency keys are unique per organization (the plan said per action); one tenant's key never meets another's.
- Two resubmissions of the same deferred or failed key at the same moment can both run their checks; writes are still kept apart by their shop's slot (both hold the same execution as holder, so the second is deferred), and reads are harmless to repeat.
- A half-open probe that is then refused by the rate budget leaves the breaker half-open until another cooldown passes.
- The Etsy budget in `registry/actions/etsy_open_api.toml` (5 per second, 5000 per day) is a conservative placeholder until the owner reads the real figures in the Developer Portal.
