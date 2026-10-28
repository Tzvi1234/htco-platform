# Phase 6 — Fulfillment monitoring and incidents

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 5 |
| **Requirements** | FR-AUT-05, FR-EML-01, FR-INC-01, FR-INC-02, FR-INC-03, FR-INC-04 (allowlist only), FR-INC-05, FR-INC-06, FR-NTF-01, FR-ORD-03, FR-ORD-05, FR-ORD-06, FR-ORD-07 |
| **Status** | In progress (started 2026-10-25, in parallel with the end of Phase 5) |

## Progress

- [x] 6.1 Tracking detection (migration `20261025100000_fulfillment_incidents.sql`, pgTAP `19_fulfillment_incidents`; [feature docs](../../features/fulfillment-monitoring/overview.md); inactive until 6.7)
- [x] 6.2 SLA policies and inspector (default window 7 business days, the owner's decision of 2026-10-23, replacing 10; schedule `fulfillment.inspect`, off by flag)
- [x] 6.3 Incidents ([feature docs](../../features/incidents/overview.md); AC-6.3.4's trigger from `sync_drifts` is `DriftIncidents`, 6.7 PR #129; the drift's shop must be the execution's own, and the guard fails closed)
- [x] 6.4 Notifications and email (migration `20261025110000_notifications.sql`, pgTAP `25_notifications`; [feature docs](../../features/notifications/overview.md); Resend built and OFF until the owner's DNS for `office.ht-co.app`)
- [x] 6.5 Incident center UI (htco-clients #48: the incident center and the notification bell, on the 6.4 API)
- [ ] 6.6 Guarded shipment write: the gateway, adapter and channel part is built (W3, every write flag off, the kill switch re-checked at execution, scope `transactions_w`, re-read before and after; fakes only). Part B built: the API routes (`/v1/shops/{shop_id}/owner-writes`: the console, the confirm-sheet previews and the Owner's press, through the gateway only; Owner, second factor and step-up checked before it; 403 on a missing capability, scope, flag or with the kill switch on; `already_present` without a send; `reconnect_url` for the re-consent). The API wires the writes for the Owner's press only (`tests/architecture/test_owner_write_wiring.py`), inert until `HTCO_ETSY_WRITE_ACTIONS` names them. Open: the 6.8 UI (lyra); the connect flow still asks only for the read scopes, so the re-consent link needs the write scopes added to it (owner decision)
- [ ] 6.7 Fulfillment automations activated. Part 1 (#111): both specifications fully defined and true of the code (AC-6.7.1, tested in `tests/tools/test_registry_files.py`), `readiness` still `draft`. Part 2: AC-6.7.2 (the schedule and event paths replayed together) and AC-6.7.3 (breach → incident → notification → email replayed end to end) tested in `tests/application/test_fulfillment_replay.py`. AC-6.7.4 built (#173: a dead-lettered execution opens its `automation_dead_lettered` incident, which notifies the Owners and Admins, and logs at ERROR; #212: the worker reports it to Sentry through the engine's `on_dead_letter`, identifiers and the stored error only). Open: AC-6.7.5 (a remediation becomes a person's task; waits for the 7.1 task model, #181), the AC-6.3.4 drift trigger, and a notification on escalation; then the owner's activation
- [ ] 6.8 Owner-triggered fulfillment writes (receipt and shipment tools), plus vacation mode (pulled forward from 7.12): the tracking upload and vacation mode are built with 6.6 (W3: the Owner's press after a step-up, or an agent's request the Owner approves each time). The write actions console's API lists every owner-triggerable write (a registry that later writes join) with its flag, kill switch, scope and last run; each run is an action execution. A relay item (6.10) uploads its pasted code through `POST /v1/relay-items/{id}/upload` and closes on the verified result. `etsy.receipts.update` (mark shipped, where the upload is blocked, A-04) is built the same way: W3, `transactions_w`, flag off, re-read before and after, `POST .../owner-writes/mark-shipped`; it waits for the owner's first press
- [x] 6.9 Ship-by SLA, holiday calendar and low-review alerts (deterministic). Ship-by and stock alerts built on the 6.4 notification path (migration `20261025120000_fulfillment_alerts.sql`, pgTAP `26`); the holidays come from the shop's SLA-policy calendar until `etsy.holiday_preferences` is read; low-review alerts (AC-6.9.2, #108): Incident Detection's hourly `reviews` mode reads `etsy.reviews.read` (R0, GET) through the gateway and alerts each review of 3 stars or fewer once, with its listing and order; no migration. Holiday preferences: the read (`etsy.policies.read`, R0, `GET .../owner-writes/holiday-preferences`) and `etsy.holiday_preferences.update` (W3 while owner-trigger first, `shops_w`, flag off, re-read before and after; the confirm sheet shows the holiday's state on Etsy now) are on the write actions console; the SLA calendar does not read them yet
- [ ] 6.10 AutoVA tracking relay (copy tracking from AutoVA to Etsy). PR A: relay items, the paste, the hand-off and close on evidence (migration `20261026160000_relay_items.sql`, pgTAP `27`, window A-20); PR B: the screen; the upload waits for 6.8

Slices 6.1 to 6.4 are built as two automations on the engine of [Phase 5](phase-05-orders-ingest.md): **Tracking Synchronization** (slice 6.1) and **Incident Detection & Escalation** (slices 6.2 to 6.4). Slice 6.7 completes their specifications and activates them.

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- Tracking Synchronization and Incident Detection & Escalation perform every business step through the Action Gateway (Internal and API channels). Neither has an AI step.
- Slice 6.6 (guarded shipment write) becomes the `etsy.shipments.create` and `etsy.receipts.update` tools, policy W2. On each shop they start **owner-trigger only**: the owner runs the first write himself (slice 6.8). Only after a verified success may Tracking Synchronization request them, and each request is an approval.
- Webhooks `order.shipped`, `order.delivered` and `inventory.out_of_stock` from slice 5.3 feed this phase's automations.
- Where tracking upload is blocked for an Israeli key (A-04), the order moves to a Human task, or to a POD partner's tracking (Phase 13, browser row only if scoped). Phase 6 never uses a browser.

## Architecture audit follow-ups (2026-10-23 review)

- **One alert model, one path (P2-2).** Incidents (6.3), alerts (6.9) and security notices all reach people through the one notification path of 6.4: a `NotificationRequest` (in the worker) or a `notifications.requested.v1` / `incidents.opened.v1` event through the outbox, then `notifications` rows and `email.send` messages. 6.9 adds no alert table of its own. Phase 10's monitors later emit into the same path. The incident's `incident_events` is that aggregate's own state history (like `automation_execution_events`), not a second activity trail; the audit and `order_status_events` stay as they are.
- **Status vocabulary (P2-2).** `IncidentState` maps to `htco_domain.status.StatusKind`, covered by the status tests.
- **Gateway bypass (P1-1).** Before any write tool lands (6.6, 6.8), an architecture test asserts that every Etsy write goes through the Action Gateway, with a documented allow-list of the existing read bypasses. Done: `tests/architecture/test_gateway_bypass.py` (#105); the webhook re-reads stay on its list as a read bypass until they move onto read actions. Phase 6's own Etsy reads (Tracking Synchronization) already go through `etsy.receipts.read`.
- **Phase 6 → 7 direction (P1-4).** Phase 6 does not depend on Phase 7. The 6.10 relay keeps its own minimal relay item (owned by Phase 6); Phase 7's task store later **extends** it (a relay item becomes a kind of task), not the other way round. AC-6.7.5's "task for a person" is the relay item / Human-channel contract of 5.13 until then.

## Goal

HTCO notices fulfillment problems before buyers do: when an order has no tracking after its realistic window, exactly one incident opens and the owner gets exactly one email; when tracking appears in Etsy, the incident closes itself on that evidence. All deterministic code, no AI: routine fulfillment processing is done by automations, not by an employee. Eitan, the Fulfillment Inspector, joins in [Phase 9](phase-09-router-advisor.md) to investigate the exceptions these automations raise.

## Scope

**In:** tracking detection from shipment records and `order.shipped` webhooks, SLA policies, the inspector job, incidents with state machine and deduplication, in-app notifications, transactional email through Resend from `noreply@office.ht-co.app`, incident center UI, guarded shipment write (only if the capability is verified).

**Out:** approvals for remediations ([Phase 7](phase-07-tasks-approvals.md)); exception investigation by Eitan ([Phase 9](phase-09-router-advisor.md)); incident remediation by Amir, the Debugger & Reliability Engineer ([Phase 19](phase-19-engineering.md)). Until Phase 19, remediation is either allowlisted, reversible, idempotent code (for example re-running a sync, FR-INC-04) or a person.

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Tracking Synchronization | [`tracking-sync.toml`](../../../registry/automations/tracking-sync.toml) | 6.1 | Eitan (the owner is accountable until Eitan is active in Phase 9) |
| Incident Detection & Escalation | [`incident-detection-escalation.toml`](../../../registry/automations/incident-detection-escalation.toml) | 6.2, 6.3, 6.4 | Amir (the owner is accountable until Amir is active in Phase 19) |

The activation rule of Phase 5 applies: all 18 specification sections defined, schemas in `packages/contracts/automations/`, replay, failure and recovery tests passing, a runbook, and the owner's approval.

## Who handles an incident, by phase

| From | Detection | Investigation | Remediation | Resolution |
|---|---|---|---|---|
| Phase 6 | Incident Detection & Escalation (deterministic) | A person | Allowlisted code or a person | On evidence only (FR-INC-05) |
| Phase 9 | Same | Eitan for fulfillment exceptions | Allowlisted code or a person; non-allowlisted fixes become approvals | Same |
| Phase 10 | Same, plus Noa's monitors and the nightly supervisor | Eitan; Noa follows up | Same | Same, verified by Noa's follow-up |
| Phase 19 | Same | Same | Amir for technical incidents, within the allowlist; everything else through approvals or engineering requests | Same |

## Owner prerequisites

- DNS for `office.ht-co.app`: SPF, DKIM and DMARC records for Resend (no MX yet).
- Resend account; sending domain verified.
- Agreement on initial SLA windows (from the Phase 0 data). The default is agreed: 7 business days (the owner's decision of 2026-10-23); per-shop, supplier and shipping-method windows remain open.

## Slices

### 6.1 — Tracking detection

Shipments from ingest become `tracking_events`; the order moves to `SHIPPED` with its tracking code recorded, plus the source and time it was observed; `fulfillment.tracking_detected.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-6.1.1 | A receipt with multiple shipments records each once; repeated observation adds no duplicates. |
| AC-6.1.2 | The order timeline shows when tracking first appeared and from which source. |

### 6.2 — SLA policies and inspector

`sla_policies` per shop, supplier and shipping method, with business-day calendars; defaults from Phase 0 measurements; scheduled inspector emits `fulfillment.sla_breached.v1` for orders past their window without tracking.

| ID | Acceptance criterion |
|---|---|
| AC-6.2.1 | An order inside its window never produces a breach (boundary tests at exactly the deadline). |
| AC-6.2.2 | Weekends and configured holidays are excluded when the policy says business days. |

### 6.3 — Incidents

Incident aggregate and state machine; deduplication (one open incident per resource and cause); automatic transition to `VERIFYING` and `RESOLVED` when evidence arrives; `incidents.opened.v1` and `incidents.resolved.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-6.3.1 | Two breaches for the same order and cause produce one incident. |
| AC-6.3.2 | When tracking appears, the incident resolves with the evidence recorded; without evidence, it never resolves. |
| AC-6.3.3 | Every illegal state transition is rejected (state-machine tests). |
| AC-6.3.4 | A drift recorded by reconciliation (Phase 5) opens one incident linked to the order. |

### 6.4 — Notifications and email

`notifications` (in-app) and `notification_preferences`; `EmailPort` with the Resend adapter; emails sent through the outbox with an idempotency key; templates in Hebrew and English; `Reply-To` set to the support address. Security notifications by email: sign-in from a new device, MFA changes, shop connected or disconnected, integration needs re-authentication, refresh token close to expiry.

| ID | Acceptance criterion |
|---|---|
| AC-6.4.1 | One incident produces one email even if the delivery job runs twice. |
| AC-6.4.2 | Email content contains no buyer PII beyond the receipt number. |
| AC-6.4.3 | Resend failures retry with backoff; permanent failures are visible in the admin view. |
| AC-6.4.4 | A sign-in from a new device sends one email to that user; MFA changes and shop connections notify the owner. |

### 6.5 — Incident center UI

Incident list (severity, shop, age, state), incident detail (evidence, timeline, related order), notification bell; links from the order timeline.

| ID | Acceptance criterion |
|---|---|
| AC-6.5.1 | Incident states use status dot plus word, never colour alone. |
| AC-6.5.2 | Screen reader announces new notifications politely, not intrusively. |

### 6.6 — Guarded shipment write

Only when the capability probe says shipment writes are allowed: an operator confirms a tracking number for an order, the system re-reads the receipt, skips if the code is already there, and otherwise sends through the outbox; 403 shows "capability blocked".

| ID | Acceptance criterion |
|---|---|
| AC-6.6.1 | Without a verified capability, the write path is unreachable (UI hidden and API returns 403). |
| AC-6.6.2 | If the tracking code is already present, no request is sent to Etsy. |
| AC-6.6.3 | Every write requires an MFA session with recent re-authentication and writes an audit record. |

### 6.7 — Fulfillment automations activated

Tracking Synchronization runs on a schedule and on `order.shipped` events (idempotency key per receipt and shipment); it reads shipment records from Etsy (tracking reaches Etsy through the relay of slice 6.10, not by itself: the owner confirmed on 2026-10-23 that he copies it by hand from AutoVA today, A-05), records tracking codes with the source of proof, and never writes a second shipment. Incident Detection & Escalation evaluates SLA rules, opens exactly one incident per root cause and resource, notifies once, and escalates by severity and age. Both specifications are completed to all 18 sections.

| ID | Acceptance criterion |
|---|---|
| AC-6.7.1 | Both specifications have no `draft` section and pass the registry loader's readiness check. |
| AC-6.7.2 | Replaying the same shipment observation N times through Tracking Synchronization (schedule and event paths together) records one tracking event, one transition to `SHIPPED` and no outbox message to Etsy. |
| AC-6.7.3 | Replaying the same breach N times through Incident Detection & Escalation yields one incident, one in-app notification and one email (extends AC-6.3.1 and AC-6.4.1 to the automation path). |
| AC-6.7.4 | When Incident Detection & Escalation itself fails until dead-lettered, the failure reaches the owner through the error-monitoring alert rules of Phase 1 and the queue health view of slice 5.9, never silently. |
| AC-6.7.5 | An escalation step that would need a non-allowlisted remediation creates a task for a person (an approval from Phase 7) and runs nothing itself. |

### 6.8 — Owner-triggered fulfillment writes

The Owner re-consents to `transactions_w` and runs `etsy.shipments.create` or `etsy.receipts.update` by hand on one real order, through the gateway (W2: step-up, flag, audit, re-read). The first result, success or Etsy's error, is shown as it is and recorded as the tool's first run on that shop.

| ID | Acceptance criterion |
|---|---|
| AC-6.8.1 | Before the owner's first verified run on a shop, every automation or employee request for these tools is refused with `owner_trigger_required`. |
| AC-6.8.2 | A missing `transactions_w` scope refuses the write before any Etsy call, with a "reconnect to grant" link. |
| AC-6.8.3 | Tests and CI never reach Etsy for these tools (fakes only); the adapter refuses a write without an owner-trigger record or a decided approval. |

### 6.9 — Ship-by SLA, holiday calendar and low-review alerts

- **Ship-by SLA:** alerts from `expected_ship_date` and processing profiles (`etsy.policies.read`), with the shop's holiday preferences in the business-day calendar. Changes to holidays use `etsy.holiday_preferences.update` (W1, owner-trigger first).
- **Low reviews:** an alert for a review of 3 stars or fewer (`etsy.reviews.read`, hourly).
- **Stock:** an out-of-stock alert from `inventory.out_of_stock`.

All deterministic ([feature backlog](../../product/FEATURE_BACKLOG.md)).

| ID | Acceptance criterion |
|---|---|
| AC-6.9.1 | An order whose ship-by date is within the warning window without a shipment raises one alert; holidays from the shop's preferences are excluded. |
| AC-6.9.2 | A new review of 3 stars or fewer raises one alert, once, with the listing and order linked. |

### 6.10 — AutoVA tracking relay

The owner orders from suppliers through AutoVA and today copies each tracking number from AutoVA into Etsy by hand (2026-10-23). AutoVA has no known API (A-06). The relay closes that loop for every paid order:

1. **Detect:** a paid order (`order.paid` webhook or reconciliation) with no shipment after its supplier window opens a relay item.
2. **Get the tracking number**, in this order of preference:
   - **Until Phase 8A:** a Human task, "Copy the tracking number from AutoVA", with the order, the AutoVA link and a field in the dashboard where the owner pastes the number and picks the carrier. The carrier and number format are validated.
   - **From Phase 8A:** the read-only browser recipe A-01 (AutoVA order status and tracking) in the owner's own AutoVA environment (password and TOTP from the vault and the Authenticator of Phase 8B), within AutoVA's terms (A-08).
   - **If AutoVA sends an email with the tracking** (A-09): the email intake of Phase 14 reads it; if confirmed early, that slice moves forward.
3. **Upload to Etsy:**
   - **API first:** `etsy.shipments.create` (W2, owner-trigger first, slice 6.8).
   - **If the API is blocked for our key (A-04):** the approved browser row B-12 ("mark as shipped with tracking" in Shop Manager, W2, the owner's approval each time, D-16).
   - **Otherwise:** a Human task with a copy button.
4. **Verify:** re-read the receipt and record the shipment with its source (`autova_manual`, `autova_browser`, `autova_email`). A mismatch opens one incident (6.3). The same number is never uploaded twice (idempotency key per receipt and tracking code).

| ID | Acceptance criterion |
|---|---|
| AC-6.10.1 | A paid order without tracking past its window produces exactly one relay item, and pasting a number in the dashboard uploads it once (replay-safe). |
| AC-6.10.2 | A tracking number already present on the Etsy receipt is never uploaded again; the relay item closes on that evidence. |
| AC-6.10.3 | When the API refuses the shipment write, the item moves to B-12 only if its flag and scope row are on; otherwise to a Human task, never silently. |
| AC-6.10.4 | The carrier and tracking formats are validated before any write; an invalid value is rejected with a clear message. |

## Test plan

Domain tests for SLA math and incident transitions; replay, failure and recovery tests for both automations on the Postgres engine; integration tests for inspector and notifications; email adapter contract tests; UI tests; manual verification with a synthetic order in a test tenant and with a real late order if one occurs.

## Security checklist

- [ ] Email sending only from verified domains; SPF, DKIM, DMARC published (DMARC starting at `p=none`, tightened later).
- [ ] No buyer PII in emails or notifications beyond identifiers.
- [ ] Shipment write gated by capability, role, MFA and audit.

## Documentation

`docs/features/fulfillment-monitoring/`, `docs/features/incidents/`, `docs/features/notifications/`; the two automation specifications completed in `registry/automations/`; runbook entries for incident alerts.

## Demo

Create a synthetic late order in a test shop; watch the incident and email arrive once; add tracking; watch it resolve.

## Exit gate

- [ ] All AC-6.x pass; SLA windows agreed with the owner; first real-world week reviewed for false alarms.
- [ ] Tracking Synchronization and Incident Detection & Escalation are `ready`, and the owner approved their activation.

## Rollback

Set Tracking Synchronization's and Incident Detection & Escalation's `readiness` back to `draft` (a registry change, merged, then the Migrate workflow's `htco registry sync --automations --apply`, because the worker reads the definitions from the database): the engine refuses every new trigger, the schedule and the `order.shipped` webhook alike; executions already queued still run, so stop the agent-worker deployment as well to stop them at once. Removing `fulfillment.inspect` from `HTCO_WORKER_SCHEDULES` stops the schedule only. Incidents remain for review.

## Risks

| Risk | Mitigation |
|---|---|
| Too many false alarms | Start with generous windows; tune from data; snooze per order |

## Cost impact

Resend free tier while volume is low.
