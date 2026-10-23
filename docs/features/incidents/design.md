# Incidents — Design

## Layers

| Layer | Module | What it holds |
|---|---|---|
| Domain | `htco_domain.incidents` | `IncidentState` and `INCIDENT_TRANSITIONS`, `Severity`, `Cause`, `ResourceKind`, `dedup_key`, `Evidence`, `resolution_path`, `should_escalate` |
| Application | `htco_application.incidents` | `Detection`, `IncidentRecord`, `IncidentStep`, the `IncidentStore` port, `IncidentService` (`open`, `resolve_on_evidence`, `move`, `open_incidents`), `ObservedDrift` and `drift_detection` |
| Persistence | `htco_persistence.fulfillment.PostgresIncidentStore` | Postgres store |

## Data

Migration `20261025100000_fulfillment_incidents.sql`:

- `app.incidents`: the aggregate. `dedup_key` is `{cause}:{resource_kind}:{resource_id}`; for an order the resource ID is `{shop_id}/{receipt_id}`. A partial unique index `(organization_id, dedup_key) where state <> 'resolved'` makes deduplication hold under concurrency (the insert is `on conflict do nothing`, then the open row is read). `details` holds identifiers and figures only (policy, deadline, drift ID), at most 4 kB. `resolved_at` is set exactly when `state = 'resolved'`.
- `app.incident_events`: every edge with its actor, time, note (a reason code) and evidence (`kind`, `source`, `reference`, `observed_at`, all or none). Append-only. A trigger refuses edges outside the machine; a `resolved` event needs evidence; a state update to `resolved` needs that event first.

## Events

Written in the incident's transaction through the outbox (`events.publish`):

| Event | When | Payload |
|---|---|---|
| `incidents.opened.v1` | A new incident | IDs, key, cause, severity, resource, receipt number, detected time, source |
| `fulfillment.sla_breached.v1` | With the opening of a breach | Receipt number, policy ID and version, window, unit, paid time, deadline |
| `incidents.resolved.v1` | Resolution | IDs, key, evidence kind, source and reference, resolved time |

The notification dispatcher (6.4) consumes `incidents.opened.v1`.

## Decisions

- The machine follows FR-INC-02 literally; evidence may arrive in any open state, so every open state has an edge to `VERIFYING`, except `REMEDIATION_PROPOSED`, whose only exits are remedies: evidence there records `DEVELOPER_REQUIRED` (handled outside HTCO) before verifying.
- `move` never resolves: resolution has its own call that needs evidence.
- Stale writes are refused (`StaleIncident`): the store locks the row and checks the state the caller read.

## The incident center API (slice 6.4, for 6.5)

`GET /v1/incidents` applies a shop-scoped role's grants in the query, before the limit, so other shops' incidents never crowd out its own (Codex review of #87).

`GET /v1/incidents` also narrows to one shop (`shop_id`) or one order (`shop_id` and `receipt_id`), so the order sheet asks for one order's incidents instead of the whole history. It pages newest first by keyset on (`detected_at`, `id`): `limit` (1 to 200, default 200) and an opaque `cursor`, with `next_cursor` null on the last page. The order sheet and the center send the organization on every call. Every filter applies in the query, before the limit.
