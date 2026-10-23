# Browser Environments — Overview

| | |
|---|---|
| **Slug** | `browser-environments` |
| **Phase / slices** | Phase 5 — slice 5.16 (Shop Environment Registry, contracts only); the browser itself in Phase 8A |
| **Decision** | [ADR-0020](../../adr/0020-per-shop-browser-environment.md) (the honest design: no anti-detect browser, no per-shop proxy), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md); design [execution-engine.md §3 and §7](../../architecture/execution-engine.md#7-state-machines) |
| **Status** | Contracts, table and state machine built and tested in CI. Creating an environment is off (the `browser_environments` flag; no creation endpoint) until Phase 8A. |
| **Last updated** | 2026-10-24 |

## What and why

A shop may have one optional browser environment, used by the Browser channel for work Etsy's API cannot do. Phase 5 defines the registry that holds it: the seven statuses and their transition table, an append-only history with actor, reason and correlation ID, and `browser.environment_status_changed.v1` for every change. The Execution Router routes a browser binding only to a `READY` environment. Adding a shop stays OAuth only.

The record is honest by design: no fingerprint, user-agent or proxy fields.

## Acceptance criteria

| ID | Criterion | Status |
|---|---|---|
| AC-5.16.1 | Every allowed transition succeeds, and every other one is refused by the domain and by the database. | Pass (domain, in memory; database in CI: pgTAP `15`, integration) |
| AC-5.16.2 | Every transition records actor, reason and correlation ID; the history is append-only. | Pass (in memory; Postgres and pgTAP in CI) |
| AC-5.16.3 | A shop of organization A cannot read or change B's environment (pgTAP). | Pass (pgTAP `15` in CI; the use case and route answer 404) |
| AC-5.16.4 | Adding a shop still needs OAuth only; a shop without an environment is `NOT_CONFIGURED`, and every API-channel action works unchanged. | Pass |

## Scope

- **In:** `htco_domain.browser` (statuses, transition table, `BrowserEnvironment`, `EnvironmentChange`, `ConsentRecord`, `HealthReport`), `htco_application.browser_environments` (`ShopEnvironmentRegistry` and `EnvironmentProvisioner` Protocols, `EnvironmentRegistry`, `MemoryEnvironmentStore`, `FakeEnvironmentProvisioner`, `EnvironmentBrowserGate`, the status read), `htco_persistence.browser_environments`, migration `20261024200000_browser_environments.sql`, `GET /v1/shops/{shop_id}/browser-environment`, the `view_browser_environment` capability (Owner, Admin).
- **Out:** any browser (8A), creating, suspending, resuming or deleting an environment through the API (8A), credentials and TOTP (8B), browser scope flags (8A), and deciding a status from health reports (8A).

## Known limits

- `EnvironmentBrowserGate` answers every browser flag and scope as missing: every Etsy browser flag is off until Phase 8A records its scope (ADR-0022). So no browser binding is chosen yet, even for a `READY` environment.
- The deployed worker does not yet use the registry: the gateway is not wired into the deployed processes (5.11).
