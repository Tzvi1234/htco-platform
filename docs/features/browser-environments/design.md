# Browser Environments — Design

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Domain | `htco_domain.browser` | `EnvironmentStatus`, the transition table (`check_environment_transition`, `environment_edges`), `BrowserEnvironment`, `EnvironmentChange`, `ConsentRecord`, `HealthReport` |
| Application | `htco_application.browser_environments` | Ports `ShopEnvironmentRegistry`, `EnvironmentStore`, `EnvironmentProvisioner`, `EnvironmentReader`; `EnvironmentRegistry`; `MemoryEnvironmentStore`; `FakeEnvironmentProvisioner`; `EnvironmentBrowserGate` (the router's `BrowserGate`); `browser_environment_status` |
| Adapters | `htco_persistence.browser_environments` | `PostgresEnvironmentStore` (API login role), `PostgresEnvironmentReader` (as the user, RLS) |
| API | `htco_api.browser_environments` | `GET /v1/shops/{shop_id}/browser-environment` |

## State machine

The table of [execution-engine.md §7](../../architecture/execution-engine.md#7-state-machines): 20 edges. A shop without a row is `NOT_CONFIGURED`; a row is created `PROVISIONING` (the edge `NOT_CONFIGURED → PROVISIONING` is the first history row); deleting sends any configured status back to `NOT_CONFIGURED`, and creating again provisions the same row. The domain checks every edge; the database checks it again with `private.browser_environment_edge_allowed`, used by the status trigger and by the history's check constraint. A unit test compares the function's pairs with the domain's.

## Contracts

- **Tables** (`20261024200000_browser_environments.sql`):
  - `app.shop_browser_environments`: one per shop; status; the consent (text version, user, time, SHA-256 of the address: all or none); the sealed profile's storage key and key ID (8A); challenge count; last health and session. No proxy, fingerprint or user-agent column.
  - `app.browser_environment_events`: append-only; `transition` rows (from, to, actor, reason, correlation) and `health` rows (ok, problem codes).
  - RLS: the Owner and Admins read their organization's environments (without consent or profile columns) and history; the worker reads its job's organization's statuses; only the API's login role writes.
- **Event:** `browser.environment_status_changed.v1` through the outbox (`events.publish`), in the same transaction as the change; IDs, statuses, actor and time only.
- **Route:** `GET /v1/shops/{shop_id}/browser-environment` → `{shop_id, status}`; 403 for roles other than Owner and Admin, 404 for a shop the caller cannot see, `Cache-Control: no-store`.

## Decisions

| Decision | Why |
|---|---|
| `transition(..., expected=...)` plus a compare-and-set in the store | Two writers that read the same status cannot both move it; the loser gets `StaleEnvironmentError` |
| `correlation_id` added to `create`, `transition` and `record_health` (not in §3's signatures) | AC-5.16.2 records it on every change |
| Health reports are stored, not acted on | Deciding `DEGRADED` or `READY` from health checks needs the real preflight (8A) |
| A new capability `view_browser_environment` (Owner, Admin) | The slice's RLS is Owner and Admin; no existing capability had exactly that grant |
