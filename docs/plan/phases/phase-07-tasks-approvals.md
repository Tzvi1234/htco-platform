# Phase 7 — Tasks, approvals and activity records

| | |
|---|---|
| **Milestone** | **M2 — Operational core (MVP-1)** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 6 |
| **Requirements** | FR-APR-01, FR-APR-02, FR-APR-03, FR-APR-04, FR-AUD-04, FR-AUD-05, FR-AUT-05, FR-IAM-06, FR-IAM-07, FR-NTF-01, FR-NTF-03, FR-TASK-01, FR-TASK-02, FR-TASK-03, FR-TASK-04 |
| **Status** | Not started |

## Progress

- [ ] 7.1 Tasks
- [ ] 7.2 Policy engine v1
- [ ] 7.3 Approvals
- [ ] 7.4 Step-up rules for approvals
- [ ] 7.5 Approvals inbox and task views
- [ ] 7.6 Notification preferences
- [ ] 7.7 Activity record store
- [ ] 7.8 Activity query API and view
- [ ] 7.9 Scheduled Reporting & Notifications automation
- [ ] 7.10 Agent Task Scheduling & Recovery automation. Part 1: the recovery decision (`htco_domain.recovery`: a recorded effect closes, restartable resumes, the rest escalates; one recovery key per lease generation) and the `agent-task-scheduling-recovery` handler over claim, effect, apply and resume ports (`htco_application.task_recovery`); AC-7.10.1 to AC-7.10.3 tested in memory. Open: the Postgres store over the 7.1 tasks (#181) and automation executions (lease generation and `skip locked` claim, the AC-7.10.3 lock test against Postgres), the activity record written with each outcome (7.7, #191), the resumer, the worker schedule, stalled tasks past their SLA and tasks whose resource arrived, the escalation's view in the web client (ER-UX-07), and the specification's 18 sections (AC-7.10.4)
- [ ] 7.11 Approvals and the Human channel on Postgres (the Phase 5 contract)
- [ ] 7.12 Owner's first Etsy writes: shop, sections, holiday tools
- [ ] 7.13 Supplier and quote model (moved from 13.1)
- [ ] 7.14 Quote mailbox intake
- [ ] 7.15 Deterministic AutoVA quote parser
- [ ] 7.16 "New product quotes" screen and notification
- [ ] 7.17 The owner's choice of a quote, and the hand-off to AutoVA
- [ ] 7.18 Write Actions console (added 2026-10-28, the owner's request)
- [ ] 7.19 Web steps the plan missed (added 2026-10-28 by the dashboard audit)

**Foundation already in code.** `htco_domain.tasks` (`BusinessTask`, `TaskState`), `htco_domain.activity` (`ActivityRecord`, `ActivityKind`, `ActivityOutcome`), `htco_domain.actors` (`ActorRef`) and policy engine v0 (`htco_application.access.policy`, tested in `tests/application/test_access_policy.py`). This phase adds their tables, APIs and screens, and extends the policy engine to employee and automation actors.

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- The approval aggregate of 7.3 implements the **shared approval contract** defined in slice 5.14 ([execution-engine.md §5](../../architecture/execution-engine.md#5-approval-contract)). It is the one approval mechanism for users, automations, employees and browser actions.
- Tasks implement the **Human channel**: manual login, unsupported authentication, CAPTCHA, refunds in Etsy, and human-only actions. A completed task resumes the waiting execution or session from its checkpoint.
- The approval policies W1, W2 and W3 of ADR-0022 map onto 7.2 and 7.4: W2 and W3 need a recent step-up, and W3 is Owner only.

## Goal

The human-in-the-loop core: a task model that every actor uses, a policy engine that decides what each actor may do, and approvals that are safe even under pressure (expired, repeated, self-approved, or approved from a stale screen). Every meaningful action, by any actor, leaves one activity record in the CRM: the single trail that the office, supervision and optimization read later ([ADR-0012](../../adr/0012-living-office-organization.md) rule 4). With this, HTCO is a usable operational dashboard and ready to host AI employees.

## Scope

**In:** tasks and task events, policy engine v1 (actor kinds `user`, `employee`, `automation`, `advisor`, `system`; capabilities, risk, autonomy, versioned policies), the `activity_records` store and its query API, the Scheduled Reporting & Notifications and Agent Task Scheduling & Recovery automations, approvals with server-side re-checks, step-up re-authentication, approvals inbox and task views in the web app, notification preferences.

**Out:** AI employees, the tool registry tables and the tool bridge ([Phase 8](phase-08-workforce-runtime.md)); routing decisions and model usage records ([Phase 9](phase-09-router-advisor.md)); monitors and the nightly supervisor ([Phase 10](phase-10-supervision.md)).

## Automations activated

| Automation | Registry | Slice | Business owner |
|---|---|---|---|
| Scheduled Reporting & Notifications | [`scheduled-reporting.toml`](../../../registry/automations/scheduled-reporting.toml) | 7.9 | Lia (the owner is accountable until Lia is active in Phase 15) |
| Agent Task Scheduling & Recovery | [`agent-task-scheduling-recovery.toml`](../../../registry/automations/agent-task-scheduling-recovery.toml) | 7.10 | Maya (the owner is accountable until Maya is active in Phase 15) |
| Supplier Quote Request & Follow-up (moved from Phase 13, 2026-10-23) | [`supplier-quote-followup.toml`](../../../registry/automations/supplier-quote-followup.toml) | 7.14–7.17 | Tom (the owner is accountable until Tom is active in Phase 13) |

The activation rule of [Phase 5](phase-05-orders-ingest.md) applies: all 18 specification sections defined, schemas, replay, failure and recovery tests, a runbook, and the owner's approval.

## Slices

### 7.1 — Tasks

Task aggregate with the full state set; `task_events`; idempotency key; SLA and owner; assignee as an `ActorRef` (a person, an employee or an automation; FR-TASK-01); API to list and view tasks.

| ID | Acceptance criterion |
|---|---|
| AC-7.1.1 | Every allowed transition is tested; every illegal one from each state is rejected. |
| AC-7.1.2 | Creating a task twice with the same idempotency key returns the first task. |
| AC-7.1.3 | A task reaches `COMPLETED` only through a recorded transition that references its evidence (a resource, an execution or an approval); no message or conversation text can complete a task. |

### 7.2 — Policy engine v1

Actor kinds from `ActorRef` (`user`, `employee`, `automation`, `advisor`, `system`; version 1.0 of the plan called them user, worker and agent), capabilities with side-effect and risk levels, autonomy levels, versioned `policies`; decisions `ALLOW`, `REQUIRE_APPROVAL`, `DENY` with reasons; every decision audited with `policy_version`.

| ID | Acceptance criterion |
|---|---|
| AC-7.2.1 | Unknown capability or actor → `DENY`. |
| AC-7.2.2 | A table-driven test covers every row of the permission matrix. |
| AC-7.2.3 | Changing a policy creates a new version; old decisions keep their version. |
| AC-7.2.4 | Employee and automation actors are evaluated with their own permission sets, never a user's (FR-IAM-07): an automation acts only under its service identity, and an employee's request runs with the employee's grants and the originating user's shop access, whichever is narrower. |
| AC-7.2.5 | An `advisor` actor has no capabilities: any action requested by it is `DENY`, so a Decision Advisor answer can never authorize anything (ADR-0015). |

### 7.3 — Approvals

Approval aggregate (action, parameters, impact, cost range, risk, evidence, rollback, expiry, requester). First producers: an Operations user's request for a guarded shipment write (decided by the owner) and bulk re-sync requests above 20 orders; employees produce approvals from Phase 8 (the first in production is Eitan, Phase 9); decide endpoint re-checks role, shop access, MFA level, recent re-authentication for high risk, expiry, policy version and that the decider is not the requester; decisions idempotent; expiry job; events.

| ID | Acceptance criterion |
|---|---|
| AC-7.3.1 | An expired approval cannot be approved and cannot execute. |
| AC-7.3.2 | The requester (a user, employee or automation identity) cannot decide its own approval, and no employee or automation can decide any approval. |
| AC-7.3.3 | Approving twice returns the first decision and executes nothing twice. |
| AC-7.3.4 | A high-risk approval without step-up re-authentication in the last 10 minutes (configurable) returns 403 with a step-up challenge. |
| AC-7.3.5 | The stored decision includes what was shown to the approver (parameters and evidence hash). |

### 7.4 — Step-up rules for approvals

The step-up mechanism from Phase 3 is applied by policy: each capability and risk level declares whether a decision needs `aal2` only or a recent step-up. Biometric step-up on Android and Windows Hello on Windows arrive in [Phase 16](phase-16-android.md) and [Phase 17](phase-17-windows.md), using device keys verified by the server.

| ID | Acceptance criterion |
|---|---|
| AC-7.4.1 | Changing a capability's step-up rule creates a new policy version, and each decision records the rule that applied. |

### 7.5 — Approvals inbox and task views

Approvals inbox with approval cards (action, impact, cost range, risk, evidence, diff, rollback, expiry countdown); task board by state and list with SLA; audit popover on decisions.

| ID | Acceptance criterion |
|---|---|
| AC-7.5.1 | Screen reader reads the approval card's action, reason, impact and risk in order. |
| AC-7.5.2 | Approve and reject buttons are disabled with an explanation when the approval is expired or the user lacks permission. |
| AC-7.5.3 | The card shows server time to expiry, not client time. |

### 7.6 — Notification preferences

Per severity and per shop; applied to in-app and email notifications.

| ID | Acceptance criterion |
|---|---|
| AC-7.6.1 | Muting a shop stops its non-critical notifications; critical security notifications cannot be muted. |

### 7.7 — Activity record store

`activity_records` table for `ActivityRecord`: kind (`ActivityKind`: task execution, automation execution, business event, tool invocation, message, decision, approval, incident, routing decision, engineering change, reconciliation, quality finding), organization, shop, actor kind and ID, action, outcome (`succeeded`, `failed`, `denied`, `pending`), a short business summary, `occurred_at`, correlation and causation IDs, task ID. Append-only grants, RLS by organization and shop, partitioned by month. An `ActivityPort` writer is called by the use cases that already write audit or events: task transitions, approvals, incidents, automation executions (Phase 5 and 6 automations start writing here), notifications. The summary is a business statement ("Opened incident for receipt 123: no tracking after 4 business days"), never model reasoning, prompts or raw payloads.

| ID | Acceptance criterion |
|---|---|
| AC-7.7.1 | Every task transition, approval decision, incident transition and automation execution writes exactly one activity record with the actor, correlation ID and outcome (a coverage test walks every use case that changes state). |
| AC-7.7.2 | The application role cannot update or delete activity records; a summary over the maximum length, without a correlation ID or with a naive timestamp is rejected (domain rules of `ActivityRecord`). |
| AC-7.7.3 | A record of organization A is invisible to organization B, and a shop-scoped record is invisible to a user without that shop (pgTAP). |
| AC-7.7.4 | No column of `activity_records` can hold a prompt, model output or raw external payload: summaries are produced by code templates, and a test fails if a writer passes free text from a model or an external source. |

### 7.8 — Activity query API and view

`GET /v1/activity` with filters by actor (`employee:shon`, `automation:etsy-order-intake`), actor kind, department, shop, task, kind, outcome and time range, cursor-paginated; `GET /v1/tasks/{task_id}/activity` for one task's trail (by correlation and causation). A web view "Activity" with the same filters and saved queries such as "what did an actor do today" and "which automations failed this week".

| ID | Acceptance criterion |
|---|---|
| AC-7.8.1 | "What did `automation:etsy-order-intake` do today" returns exactly that day's records for that actor, in the user's time zone, and nothing from shops the user is not granted. |
| AC-7.8.2 | "Which automations failed" returns every execution with outcome `failed` in the range, each linked to its execution and dead-letter state. |
| AC-7.8.3 | A task's trail shows every record linked by correlation ID in causal order, across actors. |
| AC-7.8.4 | The view states its data source and freshness; an empty result says "no recorded activity", never an inferred state. |

### 7.9 — Scheduled Reporting & Notifications automation

Builds the daily and weekly operations summary from recorded activity only: orders ingested, incidents opened and resolved, approvals pending and expired, failed and dead-lettered executions, stale shops. Delivered as an in-app notification and an email according to preferences (slice 7.6). Lia presents these reports from Phase 15; until then they go straight to the owner.

| ID | Acceptance criterion |
|---|---|
| AC-7.9.1 | Every number in a report links to the activity records or rows it was computed from; a section whose source is unavailable says so instead of showing zero. |
| AC-7.9.2 | Running the daily report twice for the same day sends one report (idempotency key per organization, report type and period). |
| AC-7.9.3 | The specification has all 18 sections defined; replay, failure and recovery tests pass. |

### 7.10 — Agent Task Scheduling & Recovery automation

Scheduled scan of tasks and automation executions: expired leases, stalled tasks past their SLA, tasks waiting on a resource that already arrived. It resumes work that is marked safely restartable, reassigns or escalates the rest, and never repeats a completed business action. From Phase 8 it also covers agent sessions; Maya and Noa use it from Phases 10 and 15.

| ID | Acceptance criterion |
|---|---|
| AC-7.10.1 | A task whose lease expired is resumed exactly once if restartable, and escalated to a person otherwise; both outcomes write an activity record. |
| AC-7.10.2 | A task whose business effect is already recorded (for example the order exists) is closed with that evidence and not run again. |
| AC-7.10.3 | Two scheduler instances never recover the same task twice (lock test). |
| AC-7.10.4 | The specification has all 18 sections defined; replay, failure and recovery tests pass. |

### 7.11 — Approvals and the Human channel on Postgres

The Phase 5 in-memory approval and Human-task contracts get their Postgres stores, events (`approvals.requested.v1`, `approvals.decided.v1`, `tasks.state_changed.v1`) and the resume path for `WAITING` executions.

| ID | Acceptance criterion |
|---|---|
| AC-7.11.1 | The gateway's approval tests from 5.14 pass unchanged against the Postgres implementation. |
| AC-7.11.2 | A Human task completed by a person resumes the waiting execution exactly once; a cancelled task cancels it, with the reason. |

### 7.12 — The owner's first Etsy writes

The first write tools reach the dashboard, owner-trigger only:

- `etsy.shop.update` (announcement, sale message, vacation mode and message);
- `etsy.sections.manage`;
- `etsy.holiday_preferences.update`.

The Owner re-consents to `shops_w`. Each tool runs through the gateway with approval policy, step-up, outbox, re-read and audit. Etsy's error is shown as it is.

| ID | Acceptance criterion |
|---|---|
| AC-7.12.1 | Turning vacation mode on and off from HTCO changes the shop and is verified by re-reading it; both runs are audited. |
| AC-7.12.2 | A write that Etsy refuses shows Etsy's error, records it, and leaves the tool owner-trigger only. |

### 7.13 — Supplier and quote model

Moved from slice 13.1 on 2026-10-23, so that suppliers, quotes and mappings have **one home** from the start ([AutoVA integration](../../architecture/autova-integration.md)). Phase 13 extends this model; it does not create another.

The model:

- `suppliers`;
- `quote_requests`: one per new product (or variant) awaiting a supplier, in state `WAITING_FOR_QUOTES` → `QUOTES_RECEIVED` → `CHOSEN` → `CONFIRMED_IN_AUTOVA` → `MAPPED`, or `CANCELLED`;
- `supplier_quotes`: supplier, unit price, currency, shipping cost, shipping method, processing and shipping times, validity, source email reference;
- `supplier_mappings`: product or variant → supplier, active from the confirmation, with the quote it came from.

| ID | Acceptance criterion |
|---|---|
| AC-7.13.1 | A product has at most one active supplier mapping; activating a new one closes the old one with a reason (state-machine and database tests). |
| AC-7.13.2 | All money is `Decimal` with currency; a quote without a currency is rejected. |

### 7.14 — Quote mailbox intake

The owner chooses the mailbox (D-23):

- **(a) recommended:** a forwarding rule that sends only AutoVA mail to `quotes@office.ht-co.app` (Resend inbound, a signed webhook into the inbox of slice 5.2);
- **(b):** read-only Gmail OAuth filtered to AutoVA's sender.

Either way, each mail is stored once in the inbox (`source = "autova_quotes"`, `event_id` = the message ID). Raw content is DC2 and quarantined: no model ever reads it. Only the confirmed AutoVA sender is accepted (A-09); anything else is quarantined and counted.

This brings forward the part of Phase 14's slice 14.1 (inbound pipeline) that quotes need: one address, no attachments beyond the quote, and the same verification. Phase 14 generalizes it for `support@` and `info@`.

Owner prerequisites:

- MX records for `office.ht-co.app` and Resend inbound (option a), or Google OAuth consent (option b);
- the forwarding rule;
- AutoVA's quote sender address (A-09);
- three sample quote emails for the parser fixtures.

| ID | Acceptance criterion |
|---|---|
| AC-7.14.1 | The same quote mail delivered twice (or forwarded twice) produces one inbox row and one parse job. |
| AC-7.14.2 | Mail from any sender other than the confirmed AutoVA address is quarantined and creates no quote. |
| AC-7.14.3 | Option (a): an unsigned or wrongly signed inbound webhook is rejected. Option (b): the token's scope is `gmail.readonly` only, and the query is limited to the AutoVA sender. |

### 7.15 — Deterministic AutoVA quote parser

The parser is code, with **no model**. It maps the mail's known layout (from the owner's sample mails) to `supplier_quotes`:

- product (by AutoVA product reference, Etsy listing ID or SKU), supplier, price, shipping cost, shipping terms and times, validity;
- every field validated by a Pydantic model;
- the quote linked to its `quote_request` (one is created if it is the product's first quote).

A mail that does not parse exactly opens a Human task "check this quote" with the mail shown in a sandboxed view. It is never guessed.

| ID | Acceptance criterion |
|---|---|
| AC-7.15.1 | Each sample mail parses to the expected quote (golden-file tests); a changed layout fails parsing and opens one Human task, never a wrong quote. |
| AC-7.15.2 | An architecture test proves the parser imports nothing from the model gateway or the agent runtime. |
| AC-7.15.3 | A quote for a product that already has an active mapping is stored as an alternative and raises no "new product" alert. |

### 7.16 — "New product quotes" screen and notification

- **The screen:** one card per product waiting for a supplier, with its quotes side by side (supplier, unit price, shipping cost, total landed cost, processing and shipping times, validity), sorted by total cost. Columns use LTR numbers in the Hebrew UI.
- **Notifications:** a new quote raises an in-app notification at once, plus an email, or the owner's morning summary if he prefers (slice 7.6 preferences).

| ID | Acceptance criterion |
|---|---|
| AC-7.16.1 | A parsed quote appears under the right product within one minute of the mail's arrival. |
| AC-7.16.2 | Totals are computed in code (Decimal) and match a property test; a quote past its validity is greyed out and cannot be chosen. |

### 7.17 — The owner's choice of a quote, and the hand-off to AutoVA

- **Choosing a quote** creates an **Owner-only approval** (policy W3, step-up) that names that quote and the price and terms shown. It is required every time.
- **On approval**, the Supplier Quote Request & Follow-up automation submits the action `autova.quotes.approve_and_confirm`:
  - until Phase 8A it routes to the **Human channel**: a task "approve this quote in AutoVA and confirm the order", with the quote and a link;
  - from Phase 8A it uses recipe **A-02** (slice 8A.10).
- **The mapping** becomes active only after AutoVA shows the order as confirmed: re-read through A-01, or the owner's confirmation in the task.
- **Payment is AutoVA's Auto Pay.** HTCO never pays.

| ID | Acceptance criterion |
|---|---|
| AC-7.17.1 | No quote is approved in AutoVA, and no mapping is activated, without a decided Owner approval whose hash matches the quote. |
| AC-7.17.2 | Approving twice confirms once (idempotency per quote). |
| AC-7.17.3 | No action, task or recipe in this flow includes a payment step; a test walks every action of the flow and finds none. |
| AC-7.17.4 | A quote request with no quote after N days (default 3) reminds the owner once, without any model call. |

### 7.18 — Write Actions console

Added on 2026-10-28 at the owner's request. Today each write tool reaches the web only on its own screen (AC-6.8.4 and AC-7.12.3 on the shop page). This slice adds one dashboard section, **Admin › Write actions**, that lists **every implemented write capability** in one place:

- **Etsy API writes:** the tracking upload (`etsy.shipments.create`), vacation mode (`etsy.shop.update`), and later the shop, sections and holiday tools of 7.12 and the shop structure writes of 12.10;
- **browser automations:** the browser rows of Phase 8A (for example B-12) once they exist, each with its scope row and flag;
- **AutoVA steps:** `autova.quotes.approve_and_confirm` (7.17, recipe A-02 from 8A.10) and any later AutoVA action.

The list is read from the action catalog (`registry/actions/`), so a write that is not in the catalog cannot appear, and one that is cannot hide. For each capability the console shows its policy (W1 to W3), its flag and kill-switch state, the scope it needs on each shop, and its last runs. From the console the Owner **runs** a capability on a chosen shop and resource with step-up and sees its **result**: the execution record, the re-read diff (before and after) and the audit entry.

**Today every write needs the Owner's approval.** A run from the console is the Owner's own press (W3), and any agent's or automation's request for the same capability waits for his approval each time (AC-6.8.1). **Later**, a per-action autonomy policy (the policy engine of 7.2, versioned) lets the Owner allow named AI employees or automations to run some capabilities without an approval each time, within limits, from this same section; that switch is built when the first employee is activated (Phase 9) and is off for every capability until the Owner turns it on.

**Registration rule (ER-UX-07).** Every newly implemented write capability registers here in the pull request that adds it: its catalog entry and its row in the console are part of that slice's web step under [ER-UX-07](../../engineering/ENGINEERING_RULES.md#13-user-experience-accessibility-and-language-er-ux). A write without a console row does not pass plan review.

| ID | Acceptance criterion |
|---|---|
| AC-7.18.1 | The console lists every write action of the catalog (API, browser and AutoVA channels) with its policy, flag, kill switch and scope state per shop; a catalog test fails when a write action has no console entry, and the console shows nothing that is not in the catalog. |
| AC-7.18.2 | The Owner runs a capability from the console only with a recent step-up, behind its flag and kill switch, through the Action Gateway; the result shows the execution record, the re-read diff and the audit entry as recorded, and Etsy's or the site's error as it was returned, never a success the re-read did not confirm (ER-UX-06). |
| AC-7.18.3 | Until a per-action autonomy policy allows it, any non-Owner request for a write capability waits for the Owner's approval each time; the console shows, per capability, which actors may run it without approval (none by default) and every change to that list is a new policy version with step-up and an audit record. |
| AC-7.18.4 | Only the Owner sees the run controls and the autonomy settings; Admins see the list and the runs read-only, and other roles do not see the section (negative tests). Tests and CI use fakes only; no console run reaches Etsy or AutoVA in tests. |

### 7.19 — Web steps the plan missed

Added on 2026-10-28 by the dashboard audit. Each item is a web step for something Phase 7 builds that had none; the tasks, approvals and notification preferences already have theirs (AC-7.5.1 to 7.5.4 and AC-7.6.2, [UI_COVERAGE](../UI_COVERAGE.md)), so they are not repeated here.

| ID | Acceptance criterion |
|---|---|
| AC-7.19.1 | **Policy engine v1 on the web (7.2).** Admin › Team › Policy shows the active policy version and its history (who changed it, when, and the diff), each capability with its risk level, step-up rule and the decision per actor kind, and the latest decisions (`ALLOW`, `REQUIRE_APPROVAL`, `DENY`) with their reasons and `policy_version`, filterable by actor and capability. It is read-only except for the Owner's changes under AC-7.4.1 and AC-7.18.3, each with step-up. |
| AC-7.19.2 | **Home's "What needs you" made real.** The Home panel lists the viewer's pending approvals and assigned tasks from the 7.3 and 7.1 APIs, most urgent first (expiry, then SLA), each opening its card in the inbox (7.5); with none it says so, and it never shows demo data. The cockpit of 15.3a later extends this panel instead of replacing it. |
| AC-7.19.3 | **The relay screen and the task board (decision at 7.1's merge).** When 7.1 turns relay items into the `relay` task kind, the plan records one choice before the web work starts: `/commerce/tracking` moves onto the task board as a filtered view, or it stays as its own screen that reads the task store. Either way a relay item is reachable from the task board and from its order, and no relay item shows in two states. |
| AC-7.19.4 | **AC-6.7.5 on the web.** An escalation that needs a non-allowlisted remediation shows on the task board as a task for a person, with the incident, the escalation step and the remediation it would have needed, and the incident's detail links to it. It waits for 7.1 (the owner's decision of 2026-10-28, [ASSUMPTIONS](../../product/ASSUMPTIONS.md) D-26). |

**Future (not in Phase 7): Telegram alerts.** The Settings › Notifications demo shows Telegram as a channel, but 7.6 covers in-app and email only. Telegram (with WhatsApp) as an alert channel comes with the morning summary's channels in [Phase 15](phase-15-office-executive.md) (15.9a), through Hermes where D-10 allows, after the channel's consent is recorded. Until then the demo marks it "Soon".

## Test plan

Domain tests (state machines, policy table with all five actor kinds), API authorization matrix, activity coverage test over every state-changing use case, pgTAP for `activity_records`, replay and recovery tests for both automations, concurrency test for double decisions, UI tests; MVP-1 acceptance stories US-01 to US-06 run end to end on the deployed system.

## Security checklist

- [ ] ASVS 2.3.5 (high-value approvals: step-up while there is a single owner; a two-person rule as a policy option once a second Admin exists), 8.3.3 (act with originating permissions), V7 session step-up.
- [ ] Approval execution re-validates everything at execution time, not only at decision time.

## Documentation

`docs/features/tasks/`, `docs/features/approvals/`, `docs/features/policy-engine/`, `docs/features/activity-records/`; the two automation specifications completed in `registry/automations/`; PERMISSION_MATRIX updated; MVP-1 demo recorded.

## Demo (MVP-1)

The six MVP-1 stories end to end with the real shop: sign-in with MFA, shop capabilities, orders with freshness, a late-tracking incident and email, an approval decided and audited, and a support user blocked from another shop. Then: the activity view answering "what did Etsy Order Intake do today" and "which automations failed this week", and the first daily operations report.

## Exit gate — Milestone M2

- [ ] All AC-7.x pass; US-01 to US-06 pass on production; owner signs off MVP-1.
- [ ] Scheduled Reporting & Notifications and Agent Task Scheduling & Recovery are `ready`, and the owner approved their activation.

## Rollback

Approvals and tasks are additive; policy versions can be reverted by activating the previous version.

## Risks

| Risk | Mitigation |
|---|---|
| Approval fatigue later when employees arrive | Clear risk levels, batching of low-risk items, pre-approved policies only after evals |
| The activity table grows fast | Monthly partitions, retention per data class, indexes on actor, shop and correlation ID |

## Cost impact

None.
