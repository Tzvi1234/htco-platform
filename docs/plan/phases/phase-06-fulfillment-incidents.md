# Phase 6 — Fulfillment monitoring and incidents

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 5 |
| **Requirements** | FR-AUT-05, FR-EML-01, FR-INC-01, FR-INC-02, FR-INC-03, FR-INC-04 (allowlist only), FR-INC-05, FR-INC-06, FR-NTF-01, FR-ORD-03, FR-ORD-05, FR-ORD-06, FR-ORD-07 |
| **Status** | In progress (started 2026-10-25, in parallel with the end of Phase 5). Audit 2026-10-28: 6.1 and 6.4 are partial (AC-6.1.2, AC-6.4.3); nothing of this phase runs in production yet, because both automations are `draft` until 6.7's activation and Resend waits for the owner's DNS |

## Progress

- [ ] 6.1 (reopened 2026-10-27: AC-6.1.2 is met on the web only with AC-6.5.3) Tracking detection (migration `20261025100000_fulfillment_incidents.sql`, pgTAP `19_fulfillment_incidents`; [feature docs](../../features/fulfillment-monitoring/overview.md); inactive until 6.7; API side done in this PR: the order detail's `tracking`)
- [x] 6.2 SLA policies and inspector (default window 7 business days, the owner's decision of 2026-10-23, replacing 10; schedule `fulfillment.inspect`, off by flag)
- [x] 6.3 Incidents ([feature docs](../../features/incidents/overview.md); AC-6.3.4's trigger from `sync_drifts` is `DriftIncidents`, 6.7 PR #129; the drift's shop must be the execution's own, and the guard fails closed)
- [ ] 6.4 (partial, audit 2026-10-28: AC-6.4.3's admin view of permanent email failures is only an RLS policy on `email_deliveries`, with no route and no screen; its web step is AC-6.5.6) Notifications and email (migration `20261025110000_notifications.sql`, pgTAP `25_notifications`; [feature docs](../../features/notifications/overview.md); Resend built and OFF until the owner's DNS for `office.ht-co.app`; API side done in this PR: `GET /v1/operations/email-failures`)
- [x] 6.5 Incident center UI (htco-clients #48: the incident center and the notification bell, on the 6.4 API)
- [ ] 6.6 Guarded shipment write: the gateway, adapter and channel part is built (W3, every write flag off, the kill switch re-checked at execution, scope `transactions_w`, re-read before and after; fakes only). Open: the API route with the confirm sheet and the re-consent (part B), the 6.8 UI (lyra)
- [ ] 6.7 Fulfillment automations activated. Part 1 (#111): both specifications fully defined and true of the code (AC-6.7.1, tested in `tests/tools/test_registry_files.py`), `readiness` still `draft`. Part 2: AC-6.7.2 (the schedule and event paths replayed together) and AC-6.7.3 (breach → incident → notification → email replayed end to end) tested in `tests/application/test_fulfillment_replay.py`. AC-6.7.4 built (#173: a dead-lettered execution opens its `automation_dead_lettered` incident, which notifies the Owners and Admins, and logs at ERROR; #212: the worker reports it to Sentry through the engine's `on_dead_letter`, identifiers and the stored error only). Open: AC-6.7.5 (a remediation becomes a person's task; waits for the 7.1 task model, #181), the AC-6.3.4 drift trigger, and a notification on escalation; then the owner's activation
- [ ] 6.8 Owner-triggered fulfillment writes (receipt and shipment tools), plus vacation mode (pulled forward from 7.12): the tracking upload and vacation mode are built with 6.6 (W3: the Owner's press after a step-up, or an agent's request the Owner approves each time). `etsy.receipts.update` waits for the owner
- [x] 6.9 Ship-by SLA, holiday calendar and low-review alerts (deterministic). Ship-by and stock alerts built on the 6.4 notification path (migration `20261025120000_fulfillment_alerts.sql`, pgTAP `26`); the holidays come from the shop's SLA-policy calendar until `etsy.holiday_preferences` is read; low-review alerts (AC-6.9.2, #108): Incident Detection's hourly `reviews` mode reads `etsy.reviews.read` (R0, GET) through the gateway and alerts each review of 3 stars or fewer once, with its listing and order; no migration
- [ ] 6.10 AutoVA tracking relay (copy tracking from AutoVA to Etsy). PR A: relay items, the paste, the hand-off and close on evidence (migration `20261026160000_relay_items.sql`, pgTAP `27`, window A-20); PR B: the screen, done (#213, the paste and copy routes; htco-clients #82, the screen at `/commerce/tracking`); the upload waits for 6.8 (part B of 6.6)

Slices 6.1 to 6.4 are built as two automations on the engine of [Phase 5](phase-05-orders-ingest.md): **Tracking Synchronization** (slice 6.1) and **Incident Detection & Escalation** (slices 6.2 to 6.4). Slice 6.7 completes their specifications and activates them.

**Audit 2026-10-28.**

- **6.1 is partial.** AC-6.1.2 is not met: the order timeline shows the `shipped` transition and its ingest source, but no API or screen reads `tracking_events` (first seen, source `etsy` or `autova_*`). The web step is AC-6.5.3.
- **6.7's progress line, corrected here** (the line is left as it is so the pull requests that edit 6.6 and 6.8 beside it, such as #220, merge cleanly): the Sentry report of AC-6.7.4 merged as **#219**, not #212; the AC-6.3.4 drift trigger (#129) and the notification on escalation (#163) are **done**, not open. Still open: AC-6.7.5 (see the owner's decision below), the Sentry new-issue alert rule and the worker's DSN in production, the runbook entries, then the owner's activation.
- **6.6 and 6.8: the write console.** Every Phase 6 write (`etsy.shipments.create`, vacation mode through `etsy.shop.update`, `etsy.receipts.update` and `etsy.holiday_preferences.update`) is runnable by the Owner from the write actions console (6.6 part B's API, then the console of 7.18), with its flag off until the owner switches it on. Policy is W3 for all of them (the owner's decision of 2026-10-26); AC-6.8.1 is rewritten for it below.
- **Overdue items now in progress.** Pull requests opened on 2026-10-28 build the items the audit found overdue: the audit-log read API (for AC-3.9.1 and the audit view), the remaining daily Etsy quota on the shop card (AC-4.1.2), tracking on the order timeline (AC-6.1.2, AC-6.5.3), the failed-email admin view (AC-6.4.3, AC-6.5.6), and 6.6 part B, the owner's write routes for `etsy.shipments.create` and `etsy.shop.update` (the confirm sheet, the re-consent and the "reconnect to grant" path; AC-6.6.1, AC-6.8.2). Each slice's tick follows its merge.
- **Owner decision 2026-10-28: AC-6.7.5 waits for 7.1.** P1-4 (Architecture audit follow-ups, below) says Phase 6 does not depend on Phase 7, and that AC-6.7.5's "task for a person" is the relay item or Human-channel contract of 5.13 until Phase 7; 6.7's progress line says AC-6.7.5 waits for the 7.1 task model (#181). The owner kept it as it is for now: AC-6.7.5 waits for 7.1, an exception to P1-4 for this one criterion, revisited when 7.1 lands ([ASSUMPTIONS](../../product/ASSUMPTIONS.md) D-26, [owner reminders](../../operations/OWNER_REMINDERS.md)). Its web step is AC-7.19.4.

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- Tracking Synchronization and Incident Detection & Escalation perform every business step through the Action Gateway (Internal and API channels). Neither has an AI step.
- Slice 6.6 (guarded shipment write) becomes the `etsy.shipments.create` and `etsy.receipts.update` tools, policy **W3** (the owner's decision of 2026-10-26, which replaced W2): the Owner's own press after a step-up runs a write, and every request from an automation or an employee waits for the Owner's approval, each time. The registry (`registry/actions/etsy_open_api.toml`) and the gateway enforce it for `etsy.shipments.create` (and `etsy.shop.update`, vacation mode); `etsy.receipts.update` is not in the registry yet and enters it as W3 when it is built (it waits for the owner, 6.8).
- Webhooks `order.shipped`, `order.delivered` and `inventory.out_of_stock` from slice 5.3 feed this phase's automations.
- Where tracking upload is blocked for an Israeli key (A-04), the order moves to a Human task, or to a POD partner's tracking (Phase 13, browser row only if scoped). Phase 6 never uses a browser.

## Architecture audit follow-ups (2026-10-23 review)

- **One alert model, one path (P2-2).** Incidents (6.3), alerts (6.9) and security notices all reach people through the one notification path of 6.4: a `NotificationRequest` (in the worker) or a `notifications.requested.v1` / `incidents.opened.v1` event through the outbox, then `notifications` rows and `email.send` messages. 6.9 adds no alert table of its own. Phase 10's monitors later emit into the same path. The incident's `incident_events` is that aggregate's own state history (like `automation_execution_events`), not a second activity trail; the audit and `order_status_events` stay as they are.
- **Status vocabulary (P2-2).** `IncidentState` maps to `htco_domain.status.StatusKind`, covered by the status tests.
- **Gateway bypass (P1-1).** Before any write tool lands (6.6, 6.8), an architecture test asserts that every Etsy write goes through the Action Gateway, with a documented allow-list of the existing read bypasses. Done: `tests/architecture/test_gateway_bypass.py` (#106); the webhook re-reads stay on its list as a read bypass until they move onto read actions. Phase 6's own Etsy reads (Tracking Synchronization) already go through `etsy.receipts.read`.
- **Phase 6 → 7 direction (P1-4).** Phase 6 does not depend on Phase 7. The 6.10 relay keeps its own minimal relay item (owned by Phase 6); Phase 7's task store later **extends** it (a relay item becomes a kind of task), not the other way round. AC-6.7.5's "task for a person" is the relay item / Human-channel contract of 5.13 until then. Exception (the owner's decision of 2026-10-28, D-26): AC-6.7.5 waits for the 7.1 task model (#181), revisited when 7.1 lands.

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
| AC-6.5.3 | **Tracking on the order timeline.** The order timeline shows each tracking event of the order: when it was first seen, its source (`etsy`, `autova_manual`, `autova_browser`, `autova_email`) and what saw it (webhook, schedule or relay), with only the code's last four characters. This is AC-6.1.2 on the web, and the order API returns these events with the order. |
| AC-6.5.4 | **SLA policies and the calendar on the web.** The shop page shows the SLA policy that applies to the shop: the window, calendar or business days, the time zone, the weekend days and the holidays, each holiday with its source (the policy, or Etsy's holiday preferences once they are read). The built-in default (7 business days) is marked as such. The Owner or an Admin edits it with step-up; each change raises its version, is audited and applies from the next inspection, and every other role sees it read-only. |
| AC-6.5.5 | **Moving an incident.** A person with the right to update incidents moves one a step along its machine from its detail, with a note. The detail offers only the steps its state allows, none to a role without that right and never a resolution without evidence, and it shows each refusal (evidence required, changed meanwhile) in words. |
| AC-6.5.6 | **Email failures on Admin › Health.** The Owner and Admins see failed and skipped emails (kind, time and status code from `email_deliveries`, never the address or the body) beside the dead `email.send` messages: the admin view that AC-6.4.3 names. |

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

The Owner re-consents to `transactions_w` and runs `etsy.shipments.create` or `etsy.receipts.update` by hand on one real order, through the gateway (W3, the owner's decision of 2026-10-26: step-up, flag, kill switch, audit, re-read). The first result, success or Etsy's error, is shown as it is and recorded as the tool's first run on that shop.

| ID | Acceptance criterion |
|---|---|
| AC-6.8.1 | (Rewritten for W3 on 2026-10-28.) The Owner's own press runs one of these tools only after a recent step-up; any other actor's request (an automation, an employee or an agent) creates an Owner approval and sends nothing to Etsy until the Owner approves that request, every time, with no standing permission after a first verified run; an Admin's trigger is refused. Tested before any Etsy call, with fakes. |
| AC-6.8.2 | A missing `transactions_w` scope refuses the write before any Etsy call, with a "reconnect to grant" link. |
| AC-6.8.3 | Tests and CI never reach Etsy for these tools (fakes only); the adapter refuses a write without an owner-trigger record or a decided approval. |
| AC-6.8.4 | **The tools' state on the shop page.** The shop page lists `etsy.shipments.create` and `etsy.receipts.update` with their state on that shop: owner-trigger only, first run verified (with its date and result), or `transactions_w` missing (with the reconnect link of AC-6.8.2), before any write is tried. The Owner's run starts from the order, behind step-up, and its result is shown as Etsy returned it. |

### 6.9 — Ship-by SLA, holiday calendar and low-review alerts

- **Ship-by SLA:** alerts from `expected_ship_date` and processing profiles (`etsy.policies.read`), with the shop's holiday preferences in the business-day calendar. Changes to holidays use `etsy.holiday_preferences.update` (W1, owner-trigger first).
- **Low reviews:** an alert for a review of 3 stars or fewer (`etsy.reviews.read`, hourly).
- **Stock:** an out-of-stock alert from `inventory.out_of_stock`.

All deterministic ([feature backlog](../../product/FEATURE_BACKLOG.md)).

| ID | Acceptance criterion |
|---|---|
| AC-6.9.1 | An order whose ship-by date is within the warning window without a shipment raises one alert; holidays from the shop's preferences are excluded. |
| AC-6.9.2 | A new review of 3 stars or fewer raises one alert, once, with the listing and order linked. |
| AC-6.9.3 | **Ship time per country on the web.** The shop's Statistics tab shows orders by destination country on a map and in an accessible table, with the median days from paid to shipped and from paid to delivered, from the stored shipments and `order.delivered` events. A country with too few orders says so instead of showing a median. |

### 6.10 — AutoVA tracking relay

The owner orders from suppliers through AutoVA and today copies each tracking number from AutoVA into Etsy by hand (2026-10-23). AutoVA has no known API (A-06). The relay closes that loop for every paid order:

1. **Detect:** a paid order (`order.paid` webhook or reconciliation) with no shipment after its supplier window opens a relay item.
2. **Get the tracking number**, in this order of preference:
   - **Until Phase 8A:** a Human task, "Copy the tracking number from AutoVA", with the order, the AutoVA link and a field in the dashboard where the owner pastes the number and picks the carrier. The carrier and number format are validated.
   - **From Phase 8A:** the read-only browser recipe A-01 (AutoVA order status and tracking) in the owner's own AutoVA environment (password and TOTP from the vault and the Authenticator of Phase 8B), within AutoVA's terms (A-08).
   - **If AutoVA sends an email with the tracking** (A-09): the email intake of Phase 14 reads it; if confirmed early, that slice moves forward.
3. **Upload to Etsy:**
   - **API first:** `etsy.shipments.create` (W3: the Owner's press, or his approval of each request, slice 6.8).
   - **If the API is blocked for our key (A-04):** the approved browser row B-12 ("mark as shipped with tracking" in Shop Manager, W2, the owner's approval each time, D-16).
   - **Otherwise:** a Human task with a copy button.
4. **Verify:** re-read the receipt and record the shipment with its source (`autova_manual`, `autova_browser`, `autova_email`). A mismatch opens one incident (6.3). The same number is never uploaded twice (idempotency key per receipt and tracking code).

| ID | Acceptance criterion |
|---|---|
| AC-6.10.1 | A paid order without tracking past its window produces exactly one relay item, and pasting a number in the dashboard uploads it once (replay-safe). |
| AC-6.10.2 | A tracking number already present on the Etsy receipt is never uploaded again; the relay item closes on that evidence. |
| AC-6.10.3 | When the API refuses the shipment write, the item moves to B-12 only if its flag and scope row are on; otherwise to a Human task, never silently. |
| AC-6.10.4 | The carrier and tracking formats are validated before any write; an invalid value is rejected with a clear message. |
| AC-6.10.5 | **Relay items on the web.** Every open relay item is listed in the dashboard, and on its order, with its state and next step; its notification opens it. A tracking item shows its supplier window and offers the next step: paste the number and pick the carrier, B-12 waiting for the owner's approval, or a Human upload with its copy button. A closed item shows its source (`autova_manual`, `autova_browser`, `autova_email`) and the re-read that verified it; from Phase 7 the same items appear in the task views of 7.5. |

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
