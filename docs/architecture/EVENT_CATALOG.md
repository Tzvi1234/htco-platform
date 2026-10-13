# Event Catalog

| | |
|---|---|
| **Status** | 1.0 — events are added in the phase that first produces them |
| **Schemas** | `packages/contracts/events/<event_type>.json` (JSON Schema 2020-12) |
| **Envelope** | See [SOFTWARE_DESIGN.md §8.4](SOFTWARE_DESIGN.md#84-event-envelope) |

## Rules

1. Names are `<context>.<fact_in_past_tense>.v<N>`, for example `orders.ingested.v1`. An event states something that happened; commands are not events.
2. Every event carries `organization_id`, `shop_id` (when commercial), `correlation_id`, `causation_id` and an `idempotency_key`.
3. Payloads contain IDs and the minimum facts consumers need; no secrets and no buyer PII beyond what the consumer is authorized to see.
4. Changing the meaning or required fields of an event creates a new version; producers emit both versions until every consumer has moved.
5. Every consumer is idempotent on `event_id`.

## Catalog

Events are published through the outbox, which arrives in Phase 5. Shop connections in Phase 4 write audit records only; their events start in Phase 5.

| Event | Producer | Consumers | Phase |
|---|---|---|---|
| `shops.connected.v1` | Shops (OAuth callback) | Capability probe, notifications | 5 |
| `shops.disconnected.v1` | Shops | Sync scheduler (stop), retention | 5 |
| `shops.capability_checked.v1` | Capability probe | UI, incidents | 5 |
| `integrations.webhook_received.v1` | Webhook ingress | Ingest router | 5 |
| `orders.drift_detected.v1` | Reconciliation | Incidents (from Phase 6) | 5 |
| `orders.ingested.v1` | Ingest job | Finance, fulfillment inspector, notifications | 5 |
| `orders.updated.v1` | Ingest job | Fulfillment inspector | 5 |
| `orders.cancelled.v1`, `orders.refunded.v1` | Ingest job | Finance, incidents (close) | 5 |
| `fulfillment.tracking_detected.v1` | Tracking worker | Order state machine, incident auto-resolution | 6 |
| `fulfillment.sla_breached.v1` | SLA inspector | Incidents | 6 |
| `incidents.opened.v1` | Incidents | Notifications, Debugger (Phase 8) | 6 |
| `incidents.resolved.v1` | Incidents | Notifications, metrics | 6 |
| `notifications.requested.v1` | Any context | Notification dispatcher (in-app, email) | 6 |
| `tasks.created.v1`, `tasks.state_changed.v1` | Tasks | Office view, metrics | 7 |
| `approvals.requested.v1` | Approvals | Notifications, waiting agent run | 7 |
| `approvals.decided.v1` | Approvals | Agent runtime (resume), audit | 7 |
| `agents.run_started.v1`, `agents.run_finished.v1` | Agent runtime | Office view, cost metrics | 8 |
| `agents.tool_denied.v1` | Policy engine | Security alerts, audit | 8 |
| `budgets.threshold_reached.v1` | Budget ledger | Notifications, router | 8 |
| `catalog.listing_audited.v1` | Listing Health | Approvals, metrics | 9 |
| `catalog.listing_updated.v1` | Listing update job | Listing Health (verify), audit | 9 |
| `catalog.draft_created.v1`, `catalog.listing_published.v1` | Publishing | Supplier onboarding, metrics | 10 |
| `sourcing.quotes_received.v1`, `sourcing.mapping_activated.v1` | Sourcing | Supplier quote agent, finance | 11 |
| `support.ticket_opened.v1`, `support.reply_drafted.v1` | Support | Agents, approvals | 12 |
| `email.received.v1` | Email ingress | Quarantined extractor, support | 12 |
