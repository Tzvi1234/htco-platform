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
- [ ] 7.10 Agent Task Scheduling & Recovery automation
- [ ] 7.11 Approvals and the Human channel on Postgres (the Phase 5 contract)
- [ ] 7.12 Owner's first Etsy writes: shop, sections, holiday tools

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
