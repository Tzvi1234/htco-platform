# Event Catalog

| | |
|---|---|
| **Status** | 2.0 — the living office ([ADR-0012](../adr/0012-living-office-organization.md)); events are added in the phase that first produces them |
| **Schemas** | `packages/contracts/events/<event_type>.json` (JSON Schema 2020-12) |
| **Envelope** | See [SOFTWARE_DESIGN.md §8.4](SOFTWARE_DESIGN.md#84-event-envelope) |

## Rules

1. Names are `<context>.<fact_in_past_tense>.v<N>`, for example `orders.ingested.v1`. An event states something that happened; commands are not events.
2. Every event carries `organization_id`, `shop_id` (when commercial), `correlation_id`, `causation_id` and an `idempotency_key`.
3. Payloads contain IDs and the minimum facts consumers need; no secrets and no buyer PII beyond what the consumer is authorized to see.
4. Changing the meaning or required fields of an event creates a new version; producers emit both versions until every consumer has moved.
5. Every consumer is idempotent on `event_id`.

## Catalog

Events are published through the outbox, which arrives in Phase 4. Shop connections arrive later (Phase 9) and publish their events through it.

| Event | Producer | Consumers | Phase |
|---|---|---|---|
| `shops.connected.v1` | Shops (OAuth callback) | Capability probe, notifications | 9 |
| `shops.disconnected.v1` | Shops | Sync scheduler (stop), retention | 9 |
| `shops.capability_checked.v1` | Capability probe | UI, incidents | 9 |
| `integrations.webhook_received.v1` | Webhook ingress | Ingest router | 4 |
| `orders.drift_detected.v1` | Etsy Order Intake (reconciliation poll) | Incidents (from Phase 10), Noa (Phase 14) | 9 |
| `orders.ingested.v1` | Etsy Order Intake (automation) | Order Deduplication & Verification, Product Sales Counters, Finance Ledger Ingestion, notifications | 4 |
| `orders.updated.v1` | Etsy Order Intake | Tracking Sync, Eitan (exceptions, Phase 10) | 4 |
| `orders.cancelled.v1`, `orders.refunded.v1` | Etsy Order Intake | Product Sales Counters, finance, incidents (close) | 4 |
| `automations.execution_finished.v1` | Automation engine | Activity records, metrics, monitors (Phase 14) | 4 |
| `automations.execution_dead_lettered.v1` | Automation engine | Incident Detection & Escalation, Noa (Phase 14) | 4 |
| `fulfillment.tracking_detected.v1` | Tracking Synchronization | Order state machine, incident auto-resolution | 10 |
| `fulfillment.sla_breached.v1` | Incident Detection & Escalation | Incidents, Eitan (Phase 10) | 10 |
| `incidents.opened.v1` | Incidents | Notifications, Eitan (Phase 10), Amir (Phase 19) | 10 |
| `incidents.resolved.v1` | Incidents | Notifications, metrics, Noa's follow-up (Phase 14) | 10 |
| `notifications.requested.v1` | Any context | Notification dispatcher (in-app, email) | 5 |
| `tasks.created.v1`, `tasks.state_changed.v1` | Tasks | Office, metrics, Agent Task Scheduling & Recovery | 5 |
| `approvals.requested.v1` | Approvals | Notifications, the waiting session | 5 |
| `approvals.decided.v1` | Approvals | Agent runtime (resume), automation engine (resume), audit | 5 |
| `activity.recorded.v1` | Activity records | Monitors, office feed, metrics | 5 |
| `workforce.profile_version_published.v1` | Registry sync | Agent runtime (reload), audit | 6 |
| `agents.session_started.v1`, `agents.session_finished.v1` | Agent runtime | Office, cost metrics, Noa's sampling | 6 |
| `agents.tool_denied.v1` | Tool bridge | Security alerts, audit, Noa | 6 |
| `agents.message_sent.v1` | Office messaging | Recipients (employees, departments, the owner), office feed | 5 |
| `routing.decided.v1` | Model router | Cost metrics, Omer (Phases 7 and 14) | 8 |
| `decisions.recorded.v1` | Decision Advisor | Benchmarks, observatory | 8 |
| `budgets.threshold_reached.v1` | Budget ledger | Notifications, router | 8 |
| `supervision.finding_opened.v1`, `supervision.finding_verified.v1` | Monitors and Noa | Maya, task owners, the daily report | 7 |
| `supervision.daily_report_ready.v1` | Nightly supervisor | Lia, Maya, notifications | 14 |
| `catalog.listing_audited.v1` | Listing Health (Daniel) | Approvals, metrics | 11 |
| `catalog.listing_updated.v1` | Listing update automation | Listing Health (verify), audit | 11 |
| `catalog.draft_created.v1`, `catalog.listing_published.v1` | Catalog Draft Creation & Publishing | Supplier onboarding, metrics | 12 |
| `sourcing.quotes_received.v1`, `sourcing.mapping_activated.v1` | Supplier Quote Request & Follow-up | Tom, finance | 13 |
| `support.ticket_opened.v1`, `support.reply_drafted.v1` | Support | Dana, approvals | 15 |
| `email.received.v1` | Email Intake & Routing | Quarantined extractor, support | 15 |
| `optimization.proposal_state_changed.v1` | Optimization | Maya, Engineering, office | 7 |
| `engineering.request_created.v1`, `engineering.change_deployed.v1` | Engineering | Omer (measurement), Noa, office | 19 |
