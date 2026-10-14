# Phase 10 — Supervision and daily reconciliation

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 9](phase-09-router-advisor.md) |
| **Requirements** | FR-AUD-02, FR-AUD-05, FR-AUT-05, FR-NTF-01, FR-ORD-09, FR-QOS-01, FR-QOS-02, FR-QOS-03, FR-QOS-04, FR-TASK-01, FR-TASK-03, NFR-OBS-01 |
| **Status** | Not started |

## Progress

- [ ] 10.1 Deterministic monitors
- [ ] 10.2 Quality findings
- [ ] 10.3 Order & Financial Reconciliation automation
- [ ] 10.4 Nightly supervisor
- [ ] 10.5 Quality Review Scheduling automation
- [ ] 10.6 Noa activated
- [ ] 10.7 Supervision views

**Foundation already in code.** `htco_domain.activity` (`ActivityKind.QUALITY_FINDING`, `RECONCILIATION`), `htco_domain.tasks` and `htco_domain.automation` give the records supervision reads; the draft specifications of Noa ([`registry/employees/noa.toml`](../../../registry/employees/noa.toml)), Order & Financial Reconciliation and Quality Review Scheduling are in `registry/`. Monitors, findings, the nightly supervisor and the views are planned in this phase.

## Goal

Nothing critical happens in HTCO without being observable from the CRM, and nothing stays broken silently. Supervision is layered ([ADR-0012](../../adr/0012-living-office-organization.md) rule 5):

1. **Deterministic monitors** check every critical flow without model calls: event completeness, state transitions, deadlines, worker heartbeats and leases, queue health, missing audit or activity records.
2. **Noa**, the Quality & Operations Supervisor, acts on the exceptions they raise, samples completed work by risk, reviews high-risk operations, and follows up until fixes are verified.
3. **Scheduled reviews**: the nightly supervisor and the daily operations report.
4. **Follow-up verification**: a finding closes only on evidence.

Rules: 100% of critical activity is observable; AI review is risk-based; no model reads every event; a completed conversation is not proof that work is complete.

## Why now

From Phase 9 an AI employee acts in production. Before more employees join (Phases 11–15), an independent function must check that work really finished, reconcile the CRM with external evidence, and recover what was interrupted. The automations and records of Phases 5–7 give the monitors something complete to check.

## Scope

**In:** monitors as scheduled deterministic checks; `quality_findings` (evidence, severity, owner, follow-up task, verification); Order & Financial Reconciliation (orders, shipments and the financial entries that exist so far, against Etsy and AutoVA where data is available; `reconciliation_runs`); the nightly supervisor (seven steps below; `daily_operations_reports`); Quality Review Scheduling; Noa's activation; findings and report views; Scenarios A, B (final part) and C.

**Out:** the full finance ledger and its reconciliation with payouts ([Phase 13](phase-13-suppliers-finance.md); this phase reconciles the `ESTIMATED` entries of Phase 5); the office's Executive Office view of Noa's findings ([Phase 15](phase-15-office-executive.md)); technical repairs by Amir ([Phase 19](phase-19-engineering.md)).

## Owner prerequisites

- Agreement on the critical flows and their deadlines (proposed: order intake, tracking, incidents, approvals, automation executions, employee sessions).
- Labelled samples for Noa's evaluation set (reviews of past work with the correct verdict).
- Approval of Noa's autonomy level (proposed: L1).
- Time of the nightly run and recipients of the daily operations report.

## Automations and employees activated

| Kind | Name | Registry | Slices |
|---|---|---|---|
| Employee | Noa, Quality & Operations Supervisor | [`noa.toml`](../../../registry/employees/noa.toml) | 10.6 |
| Automation | Order & Financial Reconciliation | [`order-financial-reconciliation.toml`](../../../registry/automations/order-financial-reconciliation.toml) | 10.3 |
| Automation | Quality Review Scheduling | [`quality-review-scheduling.toml`](../../../registry/automations/quality-review-scheduling.toml) | 10.4, 10.5 |

Automations follow the activation rule of [Phase 5](phase-05-orders-ingest.md); Noa follows the employee activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7 (22-section sheet completed, evaluation passed, autonomy approved, routing policy measured).

## Slices

### 10.1 — Deterministic monitors

A monitor is a declared check with a query, a threshold, a severity and an owner, run on a schedule by the job queue, with no model calls. Initial monitors:

| Monitor | Checks |
|---|---|
| Event completeness | Every inbox event reached a terminal execution; every paid order has intake, dedup and counter executions |
| State transitions | No record sits in a state longer than its declared limit; no transition outside its state machine |
| Deadlines | Tasks, approvals and incidents past their SLA |
| Worker heartbeats and leases | Workers without a heartbeat; executions, tasks and sessions with an expired lease |
| Queue health | Depth, age, retries and dead letters above thresholds |
| Missing records | State changes without the required audit or activity record (sampled by correlation ID) |

| ID | Acceptance criterion |
|---|---|
| AC-10.1.1 | Each monitor has a test that seeds the condition and asserts exactly one finding, and a test that seeds the healthy case and asserts none. |
| AC-10.1.2 | A monitor run makes no model call (the gateway fake records none) and its cost is only database time, measured. |
| AC-10.1.3 | A monitor that fails to run is itself reported (a missing run is detected by the next run and by the queue health view). |
| AC-10.1.4 | Repeated runs on the same condition update one open finding instead of opening new ones. |

### 10.2 — Quality findings

`quality_findings`: source (a monitor, Noa, a reconciliation run, a person), severity, resource, evidence references, owner, follow-up task, state (`OPEN`, `ASSIGNED`, `FIX_PROPOSED`, `VERIFYING`, `VERIFIED`, `DISMISSED` with reason), verification evidence. A finding closes only on evidence (FR-QOS-04). Findings that are incidents (customer or fulfillment impact) link to the Phase 6 incident instead of duplicating it.

| ID | Acceptance criterion |
|---|---|
| AC-10.2.1 | A finding cannot move to `VERIFIED` without verification evidence that references a record or an external observation newer than the fix. |
| AC-10.2.2 | Dismissing a finding requires a reason and a person or Noa as actor; a dismissed finding that recurs reopens. |
| AC-10.2.3 | Every finding has an owner and a follow-up task with an SLA; overdue follow-ups are themselves picked up by the deadlines monitor. |

### 10.3 — Order & Financial Reconciliation automation

Scheduled per shop: compares internal orders, shipments and financial entries with Etsy (and AutoVA where data exists); records each comparison as a `reconciliation_runs` row with counts, differences and evidence; opens findings for Noa on drift. Extends the Phase 5 reconciliation from "ingest what is missing" to "prove that the CRM matches".

| ID | Acceptance criterion |
|---|---|
| AC-10.3.1 | For a 200-receipt fixture with seeded differences (missing order, wrong item count, missing shipment, wrong fee), each difference produces one finding with both values; a matching set produces none. |
| AC-10.3.2 | Each run records how many records it compared and the external snapshot time, so "did every Etsy order reach the CRM" is answered from `reconciliation_runs` for any day. |
| AC-10.3.3 | The specification has all 18 sections defined; replay, failure and recovery tests pass; a failed run is retried and never reported as a clean run. |

### 10.4 — Nightly supervisor

A deterministic scheduled run (started by Quality Review Scheduling) with seven steps, each writing activity records:

1. Find unresolved, stalled and overdue tasks.
2. Find expired sessions and leases (executions, tasks, agent sessions).
3. Reconcile business status with external evidence (the latest reconciliation runs; trigger one if stale).
4. Resume or reassign safely restartable work (through Agent Task Scheduling & Recovery of Phase 7); never repeat a completed business action.
5. Open incidents for unresolved failures.
6. Create approval requests for risky repairs (anything not allowlisted).
7. Produce the daily operations report (`daily_operations_reports`), delivered by Scheduled Reporting & Notifications.

| ID | Acceptance criterion |
|---|---|
| AC-10.4.1 | **Scenario C** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): with a worker killed in the middle of an automation execution and an employee session, the nightly run finds both expired leases, resumes the restartable one exactly once, opens an incident for the one that is not restartable, and lists both in the report. |
| AC-10.4.2 | **Scenario B, final part**: an Etsy order whose intake was dead-lettered (Scenario B part 1 in Phase 5) is found by step 3, recovered through step 4 from the inbox, verified against Etsy, and its finding closes only after verification. |
| AC-10.4.3 | A repair that is not allowlisted produces an approval request and is not executed. |
| AC-10.4.4 | Running the nightly supervisor twice for the same night produces one report and no duplicate incidents, approvals or resumes. |
| AC-10.4.5 | Every number in the report links to the records it came from; a step that could not run is reported as not run, never as clean. |

### 10.5 — Quality Review Scheduling automation

Schedules the nightly supervisor, Noa's risk-based samples and high-risk reviews, and collects monitor exceptions into Noa's queue as tasks. Sampling rates come from risk: every high-risk operation (external writes, money, permissions, customer messages) is reviewed; low-risk work is sampled at a rate agreed at kickoff.

| ID | Acceptance criterion |
|---|---|
| AC-10.5.1 | Every high-risk operation of the day appears in Noa's review queue; low-risk operations are sampled at the configured rate (statistical test on fixtures). |
| AC-10.5.2 | The specification has all 18 sections defined; replay, failure and recovery tests pass. |

### 10.6 — Noa activated

Noa works from tasks created by monitors, reconciliation and scheduling: she reviews the exception or the sampled work against business requirements and external evidence, records findings with evidence, opens corrective tasks and incidents, requests approvals for risky repairs, and follows up until the fix is verified. She never marks work complete on an agent's statement, never repairs systems herself, and never reads every low-level event with a model. Activation follows the four steps of slice 9.7 (sheet, evaluation, autonomy, routing).

| ID | Acceptance criterion |
|---|---|
| AC-10.6.1 | Noa's sheet has no `draft` section; her evaluation set (reviews with known verdicts, including work that claims completion without evidence) passes its thresholds. |
| AC-10.6.2 | On the evaluation set, Noa rejects every "completed" claim that lacks evidence, and every finding she records cites evidence. |
| AC-10.6.3 | Noa's model usage scales with exceptions and samples, not with event volume: a day with ten times more orders and no exceptions costs no more than the sampling rate implies (measured). |
| AC-10.6.4 | Noa cannot run remediations, change grants or approve anything (tool bridge denials recorded). |
| AC-10.6.5 | The owner approved her autonomy level; her routing policy is measured on the evaluation set and the first production weeks. |

### 10.7 — Supervision views

Web views: findings list and detail (evidence, owner, follow-up, verification), reconciliation runs per shop and day, the daily operations report, monitor status.

| ID | Acceptance criterion |
|---|---|
| AC-10.7.1 | **Scenario A** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): for one paid order, the CRM shows its full trail across actors (automations, Eitan, Noa, people) from intake to delivery or incident, and the activity, findings and reconciliation views answer "did every Etsy order reach the CRM yesterday" and "which automations failed" without reading logs. |
| AC-10.7.2 | Monitor status shows the last run time of each monitor; a monitor that missed its schedule is shown as not run, never as healthy. |

## Test plan

Seeded-condition tests per monitor; reconciliation fixtures with seeded differences; crash tests for Scenario C (worker killed mid-execution and mid-session); end-to-end Scenario B across Phases 5 and 10; Noa's evaluation set nightly; a week of production nightly runs reviewed with the owner.

## Security checklist

- [ ] Monitors and the nightly supervisor run under service identities with read access plus the specific writes they need (findings, tasks, incidents, approvals).
- [ ] Reports and findings mask buyer PII by role.
- [ ] Noa has no write path to business records other than findings, tasks, incidents and approval requests (tested).

## Documentation

`docs/features/supervision/`, `docs/features/reconciliation/`, `docs/features/employees/noa.md`; runbook entries for every monitor; the two automation specifications and Noa's sheet completed in `registry/`.

## Demo

Kill the worker in the middle of an order intake and of Eitan's session; let the nightly supervisor run; show the resumed execution, the incident, the findings, and the daily operations report; show Noa's review of a sampled high-risk operation and a follow-up verified on evidence.

## Exit gate

- [ ] All AC-10.x pass, including Scenarios A, B (final part) and C.
- [ ] Noa is `ready`, her autonomy approved by the owner, her routing policy measured.
- [ ] Order & Financial Reconciliation and Quality Review Scheduling are `ready` and approved.
- [ ] One week of nightly reports reviewed with the owner for false alarms.

## Rollback

Noa's kill switch; monitors and the nightly supervisor disabled by flag (findings remain); reconciliation can be re-run at any time because it only reads and records.

## Risks

| Risk | Mitigation |
|---|---|
| Alert fatigue from too many findings | Thresholds tuned from the first week; one finding per condition; severity ordering in the report |
| Noa's sampling misses a class of errors | Every high-risk operation is reviewed; sampling rates revisited monthly from findings |
| The nightly run repeats a business action | Restart only work marked restartable; evidence check before resume (Phase 7 rules) |

## Cost impact

Monitors and reconciliation: database time only. Noa: model usage proportional to exceptions and samples, measured at the gate.
