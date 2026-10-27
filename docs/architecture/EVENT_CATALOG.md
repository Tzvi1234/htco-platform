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

Events are published through the outbox (`jobs.outbox`, slice 5.2); the first producers arrive with slices 5.4 and 5.10. Shop connections in Phase 4 write audit records only; their events start in Phase 5.

**In code (AUD-01).**
- A producer builds a `DomainEvent` (`htco_application.kernel`), the envelope of §8.4. `publish(event)` returns the outbox message for `events.publish`, and the producer writes it in its own transaction. The event ID is derived from the organization and the idempotency key, so the same event always has the same ID.
- Consumers read it with `read_event`, which accepts version 1 unless the consumer declares more:
  - a row written before the envelope (a flat payload with `event_type` beside the facts) reads as version 1;
  - any other version, or a message that is not an event, goes to the dead letters at once with its type and is never processed.
- An event whose name below links a schema is emitted today. `tests/contract/test_event_contracts.py` fails in four cases:
  - an event type built in the source has no schema;
  - a schema has no linked row here;
  - a link points to a missing schema;
  - a producer's test builds an event that does not validate (`tests/event_contracts.py`).
- The Action Gateway's `actions.*` events still use the flat shape until the `execution/` follow-up of #140 moves them to `publish`. Their schemas already describe the envelope.

| Event | Producer | Consumers | Phase |
|---|---|---|---|
| `shops.connected.v1` | Shops (OAuth callback) | Capability probe, notifications | 5 |
| `shops.disconnected.v1` | Shops | Sync scheduler (stop), retention | 5 |
| `shops.capability_checked.v1` | Capability probe | UI, incidents | 5 |
| `integrations.webhook_received.v1` | Webhook ingress | Ingest router | 5 |
| [`orders.drift_detected.v1`](../../packages/contracts/events/orders.drift_detected.v1.json) | Etsy Order Intake (reconciliation poll) | Incidents (from Phase 6), Noa (Phase 10) | 5 |
| [`orders.ingested.v1`](../../packages/contracts/events/orders.ingested.v1.json) | Etsy Order Intake (automation) | Order Deduplication & Verification, Product Sales Counters, Finance Ledger Ingestion, notifications | 5 |
| [`orders.updated.v1`](../../packages/contracts/events/orders.updated.v1.json) | Etsy Order Intake | Tracking Sync, Eitan (exceptions, Phase 9) | 5 |
| [`orders.cancelled.v1`](../../packages/contracts/events/orders.cancelled.v1.json), [`orders.refunded.v1`](../../packages/contracts/events/orders.refunded.v1.json) | Etsy Order Intake | Product Sales Counters, finance, incidents (close) | 5 |
| `automations.execution_finished.v1` | Automation engine | Activity records, metrics, monitors (Phase 10) | 5 |
| `automations.execution_dead_lettered.v1` | Automation engine | Incident Detection & Escalation, Noa (Phase 10) | 5 |
| [`automations.execution_state_changed.v1`](../../packages/contracts/events/automations.execution_state_changed.v1.json) | Automation engine (including `WAITING`; slice 5.18: one per recorded state change, written by the execution store with the change; execution, from and to state, status kind, actor, time) | Status views, Agent Task Scheduling & Recovery | 5 |
| [`actions.executed.v1`](../../packages/contracts/events/actions.executed.v1.json) | Action Gateway (5.12; outbox destination `events.publish`, IDs and outcome only) | Activity records, metrics, monitors (unverified writes); accepted by the worker until consumers exist | 5 |
| [`actions.approval_required.v1`](../../packages/contracts/events/actions.approval_required.v1.json) | Action Gateway (slice 5.18: the step that starts waiting for an approval) | Approvals (Phase 7), notifications | 5 |
| [`actions.handed_to_human.v1`](../../packages/contracts/events/actions.handed_to_human.v1.json) | Action Gateway, from the Execution Router's hand-off (slice 5.18: the step that hands the action to a person) | Tasks (Phase 7), notifications | 5 |
| [`actions.deferred.v1`](../../packages/contracts/events/actions.deferred.v1.json) | Action Gateway (slice 5.18: a step deferred by a breaker, the rate budget, a slot or a 429; same payload as `actions.executed.v1`) | Monitors, metrics (5.9) | 5 |
| [`catalog.listing_changed.v1`](../../packages/contracts/events/catalog.listing_changed.v1.json) | Webhook ingress (`listing.created`, `listing.updated`, `listing.deleted`) | Listing Health re-audit (11), Product Studio verification (12) | 5 |
| [`catalog.inventory_changed.v1`](../../packages/contracts/events/catalog.inventory_changed.v1.json) | Webhook ingress (`inventory.created`, `inventory.updated`, `inventory.deleted`) | Price and stock monitoring (13) | 5 |
| [`catalog.out_of_stock.v1`](../../packages/contracts/events/catalog.out_of_stock.v1.json) | Webhook ingress (`inventory.out_of_stock`) | Notifications (6), Daniel (11) | 5 |
| [`browser.environment_status_changed.v1`](../../packages/contracts/events/browser.environment_status_changed.v1.json) | Shop Environment Registry (slice 5.16: one per recorded change, in its transaction; environment, shop, from and to status, actor, time) | Notifications, monitors, the office | 5 (contract), 8A |
| `browser.challenge_detected.v1` | Browser Controller | Human task, notifications, monitors | 8A |
| `browser.export_ingested.v1` | Browser recipes (B-02, B-03, B-11) | Listing Health, finance, reports | 8A |
| `authenticator.code_viewed.v1`, `authenticator.code_injected.v1` | Authenticator (never the code) | Audit, security monitors | 8B |
| `alerts.ship_by_at_risk.v1`, `alerts.low_review.v1` | Ship-by SLA and review alerts | Notifications, Eitan (9), Dana (14) | 6 |
| [`fulfillment.tracking_detected.v1`](../../packages/contracts/events/fulfillment.tracking_detected.v1.json) | Tracking Synchronization | Order state machine, incident auto-resolution | 6 |
| [`fulfillment.sla_breached.v1`](../../packages/contracts/events/fulfillment.sla_breached.v1.json) | Incident Detection & Escalation | Incidents, Eitan (Phase 9) | 6 |
| [`incidents.opened.v1`](../../packages/contracts/events/incidents.opened.v1.json) | Incidents | Notifications, Eitan (Phase 9), Amir (Phase 19) | 6 |
| [`incidents.resolved.v1`](../../packages/contracts/events/incidents.resolved.v1.json) | Incidents | Notifications, metrics, Noa's follow-up (Phase 10) | 6 |
| [`incidents.escalated.v1`](../../packages/contracts/events/incidents.escalated.v1.json) | Incidents (an incident moved to `escalated`, by the age rule or a person; once per escalation, keyed by its `escalation_id`) | Notifications (in-app and email to the Owners and Admins) | 6 |
| [`notifications.requested.v1`](../../packages/contracts/events/notifications.requested.v1.json) | Any context | Notification dispatcher (in-app, email) | 6 |
| `tasks.created.v1`, `tasks.state_changed.v1` | Tasks | Office, metrics, Agent Task Scheduling & Recovery | 7 |
| `approvals.requested.v1` | Approvals | Notifications, the waiting session | 7 |
| [`approvals.decided.v1`](../../packages/contracts/events/approvals.decided.v1.json) | Approvals (written with the decision or the expiry, in its transaction; AUD-03: approval, approved/rejected/expired (one event per state), waiting execution, the action it waits on, decider) | `ApprovalResumer` on the worker's `events.publish` (resumes or cancels the waiting execution once, for that action's wait only), agent runtime (resume, Phase 8), audit | 5 (contract), 7 |
| `activity.recorded.v1` | Activity records | Monitors, office feed, metrics | 7 |
| `workforce.profile_version_published.v1` | Registry sync | Agent runtime (reload), audit | 8 |
| `agents.session_started.v1`, `agents.session_finished.v1` | Agent runtime | Office, cost metrics, Noa's sampling | 8 |
| `agents.tool_denied.v1` | Tool bridge | Security alerts, audit, Noa | 8 |
| `agents.message_sent.v1` | Office messaging | Recipients (employees, departments, the owner), office feed | 8 |
| `routing.decided.v1` | Model router | Cost metrics, Omer (Phase 18) | 9 |
| `decisions.recorded.v1` | Decision Advisor | Benchmarks, observatory | 9 |
| `budgets.threshold_reached.v1` | Budget ledger | Notifications, router | 9 |
| `supervision.finding_opened.v1`, `supervision.finding_verified.v1` | Monitors and Noa | Maya, task owners, the daily report | 10 |
| `supervision.daily_report_ready.v1` | Nightly supervisor | Lia, Maya, notifications | 10 |
| `catalog.listing_audited.v1` | Listing Health (Daniel) | Approvals, metrics | 11 |
| `catalog.listing_updated.v1` | Listing update automation | Listing Health (verify), audit | 11 |
| `catalog.draft_created.v1`, `catalog.listing_published.v1` | Catalog Draft Creation & Publishing | Supplier onboarding, metrics | 12 |
| `sourcing.quotes_received.v1`, `sourcing.mapping_activated.v1` | Supplier Quote Request & Follow-up | The quote screen and notifications (7), Tom (13), finance | 7 (moved from 13) |
| `sourcing.quote_chosen.v1` | The owner's approval of a quote (7.17) | AutoVA hand-off (Human task, then A-02) | 7 |
| `autova.order_status_changed.v1` | Recipe A-01 | Tracking relay (6.10), Unpaid alert, Eitan (9) | 8A |
| `support.ticket_opened.v1`, `support.reply_drafted.v1` | Support | Dana, approvals | 14 |
| `email.received.v1` | Email Intake & Routing | Quarantined extractor, support | 14 |
| `optimization.proposal_state_changed.v1` | Optimization | Maya, Engineering, office | 18 |
| `engineering.request_created.v1`, `engineering.change_deployed.v1` | Engineering | Omer (measurement), Noa, office | 19 |
