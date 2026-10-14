# Phase 14 — Governance at full scope

| | |
|---|---|
| **Milestone** | **M5 — Governance** |
| **Repositories** | both (`htco-platform` for monitors, Night Shift, the data mart and the proposal pipeline; `htco-clients` for the Quality Center, Night Shift and Optimization Center screens) |
| **Size** | M |
| **Depends on** | [Phase 13](phase-13-procurement-finance.md) (ledger, Order & Financial Reconciliation, real cost data); the pilot scope of Noa and Omer from [Phase 7](phase-07-pilot-office.md) |
| **Requirements** | FR-AGT-09, FR-AUD-02, FR-AUD-04, FR-AUD-05, FR-AUT-05, FR-FIN-03, FR-MDL-05, FR-NTF-01, FR-OPT-01, FR-OPT-02, FR-OPT-03, FR-QOS-01, FR-QOS-02, FR-QOS-03, FR-QOS-04, FR-TASK-01, FR-TASK-03, NFR-OBS-01 |
| **Mandatory tests owned** | Test 5 at full scope (completion checked against Etsy, AutoVA and the CRM); test 6, measurement part (measured savings); regression of test 3 (no model or quota: deterministic checks keep running) |
| **Status** | Planned |

Rolling wave: this phase is planned at the level of scope, slices and gates. The acceptance criteria below are the minimum; they are refined at kickoff with what the pilot (Phase 7) and the commerce phases (9–13) taught us.

## Progress

- [ ] 14.1 Deterministic monitors at full scope
- [ ] 14.2 Quality findings at full scope and risk-based sampling
- [ ] 14.3 Night Shift (Recovery Scheduler) and the end-of-day report
- [ ] 14.4 Noa at full scope
- [ ] 14.5 Data mart for optimization and the Product Metrics Rollup automation
- [ ] 14.6 Proposal pipeline at full scope
- [ ] 14.7 Omer at full scope
- [ ] 14.8 Measurement, verification and rollback (test 6, measurement part)
- [ ] 14.9 Ten failure-and-recovery scenarios
- [ ] 14.10 Quality Center, Night Shift and Optimization Center screens

**Foundation already in place before this phase.** `htco_domain.activity` (`ActivityKind.QUALITY_FINDING`, `RECONCILIATION`), `htco_domain.tasks`, `htco_domain.automation` and `htco_domain.improvement` (the proposal lifecycle `DETECTED → PROPOSED → EVALUATING → APPROVED / REJECTED → DEVELOPMENT → TESTING → DEPLOYED → MEASURING → VERIFIED / ROLLED_BACK`, tested in `tests/domain/test_lifecycles.py`). From [Phase 5](phase-05-tasks-messages-approvals.md): the task dispatcher (lease, heartbeat, retry, idempotency), Agent Task Scheduling & Recovery, Scheduled Reporting. From [Phase 7](phase-07-pilot-office.md): Noa and Omer in pilot scope, `quality_findings` with the "done without evidence" check (test 5, CRM part), the first `optimization_proposals` and the GitHub improvement issue (test 6, proposal part), Quality Review Scheduling and Optimization Review Scheduling. From [Phase 13](phase-13-procurement-finance.md): Order & Financial Reconciliation and `reconciliation_runs`. Confirm at kickoff what each of those phases actually delivered, and extend it; do not rebuild it.

## Goal

Nothing critical happens in HTCO without being observable from the CRM, nothing stays broken silently, and the office gets cheaper per unit of work with evidence. Supervision is layered ([ADR-0012](../../adr/0012-living-office-organization.md) rule 5):

1. **Deterministic collection.** Monitors collect **all** critical activity events and every exception without model calls: queue anomalies, task leases and heartbeats, missing audit or activity records, changes without an author, SLA breaches, failed automations and ledger mismatches.
2. **Noa** (Quality & Operations Supervisor) receives the collected exceptions, never the raw log. She samples completed work by risk, reviews high-risk operations, verifies that a task marked done really happened in Etsy, AutoVA and the CRM, checks that fixes hold, and signs the end-of-day report. She never accepts a session's `DONE` as proof.
3. **Night Shift.** A Recovery Scheduler scans unfinished and expired work every night, restarts what is safe to restart under its `idempotency_key`, opens an incident where the situation is sensitive, and hands Noa the evidence for the end-of-day report.
4. **Omer** (Continuous Optimization Officer) reads a data mart of token cost, latency, tool calls, automation candidates, 429s, retry cost, defect recurrence and cost per unit, and runs proposals through the full pipeline to a measured `VERIFIED` or `ROLLED_BACK`.

**Deterministic checks keep running with no model and no quota.** If the Anthropic API is unavailable, returns 429, or the AI budget is exhausted, the monitors, the Night Shift recovery, the reconciliation and the end-of-day report still run; only Noa's and Omer's judgment steps wait ([ADR-0017](../../adr/0017-auth-and-billing-policy.md) rule 3).

## Why now

By the end of Phase 13 the office runs real commerce: orders (4, 9), fulfillment (10), listings (11, 12), supplier quotes and the ledger (13), with eleven employees active. The pilot proved the supervision and optimization ideas on a small scale; now there is enough real activity, money and cost data to supervise all of it and to optimize with real baselines. This phase closes milestone **M5 Governance**: ten failure-and-recovery scenarios pass, and one proposal goes from detection to pull request to measured savings.

## Scope

**In:**

- Monitors over every critical flow (order intake, fulfillment and tracking, incidents, listings and publishing, supplier quotes, ledger, approvals, automation executions, agent runs, employee messages), declared as data with a query, threshold, severity and owner.
- Risk-based sampling of completed work; review of every high-risk operation.
- Verification of task completion against Etsy, AutoVA and the CRM through the reconciliation and sync records of Phases 9, 10 and 13.
- Night Shift (Recovery Scheduler), `daily_operations_reports` and the end-of-day report delivered by Scheduled Reporting.
- Noa's activation at full scope.
- The optimization data mart; the new **Product Metrics Rollup** automation; Optimization Review Scheduling at full scope.
- The full proposal pipeline with measurement, quality guards, `VERIFIED` and `ROLLED_BACK`; Omer's activation at full scope.
- Ten failure-and-recovery scenarios as an automated suite (slice 14.9).
- Office scenarios SCN-A, SCN-B (final part), SCN-C and SCN-D ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)).
- Web screens: Quality Center (clients specification screen 24), Night Shift, Optimization Center (screen 25).

**Out:**

- Engineering employees implementing proposals (Alex and Iris, [Phase 19](phase-19-engineering.md)). Until then a person implements approved proposals through the same issue, pull request, CI and release gates; test 6 is completed with an engineering employee's pull request in Phase 19.
- Technical repairs by Amir ([Phase 19](phase-19-engineering.md)).
- Routing experiments and model A/B tests ([Phase 21](phase-21-growth-scale.md)).
- Fitting these screens into the complete office shell and the anti-omission audit ([Phase 16](phase-16-web-office.md)).
- Marketing analytics on top of product metrics (Gal, [Phase 21](phase-21-growth-scale.md)).

## Owner prerequisites

- Agreement on the list of critical flows, their SLAs and the severity of each monitor (proposal prepared by the agent from the pilot data).
- Sampling rates for low-risk work, per operation kind.
- Labelled samples for Noa's full-scope evaluation set (past work with the correct verdict, including claimed-but-not-done work found in Phases 9–13).
- Agreement on the units of work and their cost targets (cost per listing, per order, per ticket, per image, per incident; master plan §5).
- Approval of Noa's and Omer's autonomy levels at full scope (proposed: Noa L1, Omer L0), and of Product Metrics Rollup.
- Night Shift time window, and recipients and delivery time of the end-of-day report.
- Which repairs Night Shift may perform without approval (the allowlist); everything else needs approval.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`; for slice 14.10 in `htco-clients` also load `design-system`.
2. Read, in order: this file; [PLAN.md](../PLAN.md) §4–§6; [Phase 7](phase-07-pilot-office.md) (what Noa and Omer already do); [ADR-0012](../../adr/0012-living-office-organization.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md); [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios); [ENGINEERING_RULES §8](../../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai); `DATA_MODEL.md`, `EVENT_CATALOG.md`, `PERMISSION_MATRIX.md`; the registry files [`noa.toml`](../../../registry/employees/noa.toml), [`omer.toml`](../../../registry/employees/omer.toml), [`quality-review-scheduling.toml`](../../../registry/automations/quality-review-scheduling.toml), [`optimization-review-scheduling.toml`](../../../registry/automations/optimization-review-scheduling.toml), [`agent-task-scheduling-recovery.toml`](../../../registry/automations/agent-task-scheduling-recovery.toml), [`order-financial-reconciliation.toml`](../../../registry/automations/order-financial-reconciliation.toml).
3. At kickoff, list what Phases 5, 7 and 13 delivered for monitors, findings, proposals and reconciliation. Write the gap list in the phase PR description before writing code.
4. Work the slices in order: 14.1 → 14.2 → 14.3 (the deterministic layer first), then 14.4 (Noa), then 14.5 → 14.6 → 14.7 → 14.8 (Omer's chain), then 14.9 (the scenario suite, which needs all the rest), then 14.10 (screens; may start in parallel with 14.5 once the APIs of 14.1–14.3 are in the contract).
5. For each slice: acceptance criteria → failing tests → code → verify for real against the local stack and staging → docs → one pull request.
6. Create the registry document `registry/automations/product-metrics-rollup.toml` with all 18 sections before writing its code; it passes `just registry` and the registry tests.
7. Never in this phase: let a model read every event or log line; let Noa repair, change money, approve or close a finding without evidence; let Omer change code, configuration, routing policies, budgets or deployments, or decide his own proposal; let Night Shift repeat a completed business action or run a repair outside the allowlist; report a step that did not run as clean; use a personal subscription for any hosted run (`API_PRODUCTION` only); hard-code a model ID or a price.
8. Stop and ask the owner when: a monitor would need a model to decide; a recovery is not on the allowlist and has no approval path; a reconciliation shows a money difference you cannot explain from records; the first week of nightly reports has more false alarms than the agreed limit; a measurement is inconclusive and someone wants to call it a success; an employee evaluation misses its thresholds.

## Employees and automations

| Kind | Name | Registry | Slices |
|---|---|---|---|
| Employee | Noa, Quality & Operations Supervisor (full scope; pilot scope since Phase 7) | [`noa.toml`](../../../registry/employees/noa.toml) | 14.4 |
| Employee | Omer, Continuous Optimization Officer (full scope; pilot scope since Phase 7) | [`omer.toml`](../../../registry/employees/omer.toml) | 14.7 |
| Automation | Product Metrics Rollup (**new**) | `product-metrics-rollup.toml` (created in 14.5) | 14.5 |
| Automation | Quality Review Scheduling (full scope) | [`quality-review-scheduling.toml`](../../../registry/automations/quality-review-scheduling.toml) | 14.2, 14.3 |
| Automation | Optimization Review Scheduling (full scope) | [`optimization-review-scheduling.toml`](../../../registry/automations/optimization-review-scheduling.toml) | 14.6 |

Employees follow the employee activation rule of [Phase 7](phase-07-pilot-office.md) (sheet with no `draft` section, evaluation passed, autonomy approved, routing measured); automations follow the automation activation rule of [Phase 4](phase-04-durable-core-order-intake.md). Expanding a pilot employee to full scope is a new profile version with a new evaluation, not an edit of the pilot version.

## Slices

### 14.1 — Deterministic monitors at full scope

A monitor is declared data: a query, a threshold, a severity, an owner and a schedule, run by the job queue under a service identity, with no model call. Monitor runs are recorded (`monitor_runs`, name confirmed in DATA_MODEL at kickoff). Initial set:

| Monitor | Checks |
|---|---|
| Event completeness | Every inbox event reached a terminal execution; every paid order has intake, dedup and counter executions; every shipment has a tracking execution |
| State transitions | No record sits in a state longer than its declared limit; no transition outside its state machine |
| SLA | Tasks, approvals, incidents, tickets and finding follow-ups past their SLA |
| Leases and heartbeats | Workers without a heartbeat; executions, business tasks and agent runs with an expired lease |
| Queue anomalies | Depth, age, retries and dead letters above thresholds; a queue that stopped moving |
| Missing audit | State changes without the required audit or activity record (by correlation ID) |
| Authorless changes | Business, registry, policy or configuration changes with no actor, or with an actor kind not allowed to make that change |
| Failed automations | Executions in `FAILED` or dead-letter state, per automation and shop |
| Ledger mismatches | Differences found by Order & Financial Reconciliation between orders, ledger, fees and payouts |
| Agent run integrity | `agent_runs` without `billing_mode`, without cost settlement, or with a terminal state but no terminal tool call |

| ID | Acceptance criterion |
|---|---|
| AC-14.1.1 | Each monitor has a test that seeds the condition and asserts exactly one finding, and a test that seeds the healthy case and asserts none. |
| AC-14.1.2 | A monitor run makes no model call (the gateway fake records none) and its cost is database time only, measured per run. |
| AC-14.1.3 | With the model gateway disabled, the AI budget exhausted and the Claude provider returning 429, every monitor still runs on schedule and records its result (regression of master plan test 3). |
| AC-14.1.4 | A monitor that fails to run is itself reported: the next run and the queue view show it as not run, never as healthy. |
| AC-14.1.5 | Repeated runs on the same condition update one open finding instead of opening new ones. |
| AC-14.1.6 | Every critical flow agreed with the owner has at least one monitor; CI fails when a registered automation or a critical table has no monitor mapping. |

### 14.2 — Quality findings at full scope and risk-based sampling

`quality_findings` (from Phase 7) gets the full lifecycle: source (monitor, Noa, reconciliation run, Night Shift, a person), severity, resource, evidence references, owner, follow-up task with an SLA, state (`OPEN`, `ASSIGNED`, `FIX_PROPOSED`, `VERIFYING`, `VERIFIED`, `DISMISSED` with reason) and verification evidence. Findings with customer or fulfillment impact link to the incident of [Phase 10](phase-10-fulfillment-incidents.md) instead of duplicating it. Quality Review Scheduling puts every high-risk operation of the day (external writes, money, permissions, customer messages, publishing) in Noa's queue and samples low-risk work at the agreed rates.

| ID | Acceptance criterion |
|---|---|
| AC-14.2.1 | A finding cannot move to `VERIFIED` without verification evidence that references a record or an external observation newer than the fix (FR-QOS-04). |
| AC-14.2.2 | Dismissing a finding needs a reason and a person or Noa as actor; a dismissed finding that recurs reopens. |
| AC-14.2.3 | Every finding has an owner and a follow-up task with an SLA; overdue follow-ups are picked up by the SLA monitor. |
| AC-14.2.4 | Every high-risk operation of the day appears in Noa's review queue; low-risk operations are sampled at the configured rate (statistical test on fixtures). |
| AC-14.2.5 | Quality Review Scheduling at full scope: all 18 specification sections defined; replay, failure and recovery tests pass. |

### 14.3 — Night Shift (Recovery Scheduler) and the end-of-day report

A deterministic nightly run, started by Quality Review Scheduling, using Agent Task Scheduling & Recovery ([Phase 5](phase-05-tasks-messages-approvals.md)) for the restarts. Each step writes activity records and a `recovery_runs` row (name confirmed at kickoff):

1. Find unresolved, stalled and overdue tasks.
2. Find expired leases: automation executions, business tasks, agent runs.
3. Reconcile business status with external evidence (the latest reconciliation and sync runs; trigger one if stale).
4. Restart safely restartable work under its `idempotency_key`; an agent step restarts from its task brief, never from a session ([ADR-0016](../../adr/0016-claude-native-agent-runtime.md)). Never repeat a completed business action.
5. Open incidents for unresolved failures and for anything sensitive (money, customer messages, publishing, permissions).
6. Create approval requests for repairs that are not on the allowlist.
7. Build the end-of-day report (`daily_operations_reports`, which replaces the pilot's `pilot_review_reports`; old rows stay readable); Noa reviews the business evidence and signs it; Scheduled Reporting delivers it.

If Noa's review cannot run (no model, no quota, Noa switched off), the report is still produced and delivered, marked "Noa review not run" with the reason.

| ID | Acceptance criterion |
|---|---|
| AC-14.3.1 | **SCN-C**: with a worker killed in the middle of an automation execution and of an agent step, Night Shift finds both expired leases, restarts the restartable one exactly once, opens an incident for the one that is not restartable, and lists both in the report. |
| AC-14.3.2 | **SCN-B, final part**: an Etsy order whose intake was dead-lettered is found by step 3, recovered from the inbox through step 4, verified against Etsy, and its finding closes only after verification. |
| AC-14.3.3 | A repair that is not allowlisted produces an approval request and is not executed. |
| AC-14.3.4 | Running Night Shift twice for the same night produces one report and no duplicate incidents, approvals or restarts. |
| AC-14.3.5 | Every number in the report links to the records it came from; a step that could not run is reported as not run, never as clean. |
| AC-14.3.6 | With the model gateway disabled, Night Shift completes steps 1–7 except Noa's signature, and the report says so. |

### 14.4 — Noa at full scope

Noa works from tasks created by monitors, sampling, reconciliation and Night Shift. She checks each exception or sample against business requirements and external evidence (Etsy through the Phase 9 sync, AutoVA through the Phase 10 watch, the ledger and reconciliation of Phase 13), records findings with evidence, opens corrective tasks and incidents, asks for approval of risky repairs, follows up until the fix is verified, and signs the end-of-day report. Her grants stay read-mostly: `activity.query`, `tasks.read`, `tasks.create`, `quality.record_finding`, `incidents.open`, `orders.read`, `observability.query_logs`, plus the advisor and model tools; any change to these grants is confirmed in the design review.

| ID | Acceptance criterion |
|---|---|
| AC-14.4.1 | Noa's full-scope profile version has no `draft` section; her evaluation set (with known verdicts, including work that claims completion without evidence in the CRM or in an external system) passes its thresholds. |
| AC-14.4.2 | **Master plan test 5, full scope**: a task marked done whose result is missing in Etsy, in AutoVA or in the CRM produces a quality finding with the missing evidence named; Noa never closes a task on a session's `DONE`. |
| AC-14.4.3 | Noa's model usage scales with exceptions and samples, not with event volume: a day with ten times more orders and no exceptions costs no more than the sampling rate implies (measured). |
| AC-14.4.4 | Noa cannot run remediations, change money, change grants or approve anything (tool bridge denials recorded). |
| AC-14.4.5 | When the provider returns 429 or the budget is exhausted, Noa's tasks move to `WAITING_FOR_EXTERNAL` with a retry time; nothing she owns fails silently. |
| AC-14.4.6 | The owner approved her full-scope autonomy; her routing policy is measured on the evaluation set and the first production weeks. |

### 14.5 — Data mart for optimization and the Product Metrics Rollup automation

Deterministic rollup jobs build a reporting data mart (a separate schema of daily tables; names confirmed in DATA_MODEL at kickoff) from `agent_runs`, `model_usage`, `routing_decisions`, `tool_invocations`, `automation_executions`, `activity_records`, `quality_findings`, incidents and the ledger. Measures: token cost, latency, tool calls, 429s and other provider errors, retry cost, defect recurrence, automation candidates and cost per unit (per listing, order, ticket, image, incident; per employee, department, workflow and shop). Cost is attributed to units of work through task and correlation IDs; `billing_mode` is kept, and API cost is never mixed with development subscription usage.

**Product Metrics Rollup** (new automation, `registry/automations/product-metrics-rollup.toml`): a scheduled, model-free rollup of product KPIs per shop and product (Etsy's basic listing stats, orders, units, revenue and margin from the ledger, returns, funnel steps that the data supports). Business owner: Roni; Gal joins as owner in [Phase 21](phase-21-growth-scale.md). It feeds cost per unit for Omer and the Finance and Marketing screens.

| ID | Acceptance criterion |
|---|---|
| AC-14.5.1 | Every metric states its formula, source tables and as-of time; cost figures reconcile with `model_usage` and the ledger for the same period. |
| AC-14.5.2 | "Automation candidate" is found by a deterministic rule (the same employee, task type and inputs producing the same decision repeatedly), not by a model reading every session. |
| AC-14.5.3 | 429s, retries and their cost are attributed to the employee, task type and provider route that caused them. |
| AC-14.5.4 | Product Metrics Rollup has all 18 specification sections defined; replay, failure and recovery tests pass; a rerun for the same day replaces that day's rows and never double-counts a sale. |
| AC-14.5.5 | The mart and the rollup make no model call; buyer PII never enters the mart. |

### 14.6 — Proposal pipeline at full scope

`optimization_proposals` and `optimization_measurements` get the full lifecycle. The clients specification names the stages Detected → Proposed → Evaluated → Approved → Development → Tested → Deployed → Measuring → Verified or RolledBack; they map one to one to `htco_domain.improvement` (`EVALUATING`, `TESTING`); the UI uses the specification's labels. Each proposal carries baseline, hypothesis, cost and benefit, scope, experiment design, risk, owner, quality gate, rollback plan, acceptance criteria and `deterministic_candidate` (master plan §8). Optimization Review Scheduling computes the metrics on schedule, triggers Omer's periodic analysis, and starts a review when a metric crosses its threshold. Approved proposals become an `engineering_requests` row (from Phase 7) whose outbox delivery opens one GitHub issue, linked to the proposal; engineering employees take these issues from Phase 19.

| ID | Acceptance criterion |
|---|---|
| AC-14.6.1 | Every transition follows `htco_domain.improvement`; a proposal without acceptance criteria and a rollback plan cannot reach `PROPOSED`; every transition writes an activity record. |
| AC-14.6.2 | Only Maya or the owner can move a proposal to `APPROVED` or `REJECTED`; Omer cannot decide his own proposal (policy test). |
| AC-14.6.3 | Optimization Review Scheduling at full scope: all 18 sections defined; replay, failure and recovery tests pass; a threshold crossing triggers one review, not one per run. |
| AC-14.6.4 | An approved proposal links to exactly one engineering request, and the pull request that implements it links back to the proposal. |

### 14.7 — Omer at full scope

Omer reads the mart and activity and writes proposals through `optimization.propose`; for approved proposals he creates the engineering request through `engineering.create_request`; after deployment he writes the measurement and a recommendation. He has no tool that changes code, configuration, routing policies, budgets or deployments.

| ID | Acceptance criterion |
|---|---|
| AC-14.7.1 | Omer's full-scope profile version has no `draft` section; his evaluation set (cases with known waste and known non-issues) passes; the owner approved his autonomy; his routing policy is measured. |
| AC-14.7.2 | Every proposal cites a baseline from recorded data with an evidence link; a proposal with an unsupported number fails validation. |
| AC-14.7.3 | Omer's attempts to change a policy, a grant, a budget or a deployment are denied by the tool bridge and recorded. |

### 14.8 — Measurement, verification and rollback (test 6, measurement part)

After deployment the proposal moves to `MEASURING`. The measurement compares the agreed metric over a comparable period and checks quality guards (rejection rates, findings, incidents, defect recurrence). Omer recommends `VERIFIED` or `ROLLED_BACK`; Maya or the owner decides; a rollback uses the change's own rollback plan.

| ID | Acceptance criterion |
|---|---|
| AC-14.8.1 | **Master plan test 6, measurement part / SCN-D**: Omer detects repeated model reasoning that a rule can replace and proposes it with a baseline; Maya prioritizes; the change is implemented through a pull request with an equivalence test (the rule gives the same decisions as the model on the recorded cases) and CI green; it is deployed behind a flag in stages; the measurement shows lower cost per unit with no quality loss, and the proposal reaches `VERIFIED`. |
| AC-14.8.2 | The rollback path is exercised once on a second proposal and reaches `ROLLED_BACK` with the reason recorded. |
| AC-14.8.3 | A proposal reaches `VERIFIED` only when its acceptance criteria are met and no quality guard regressed. |
| AC-14.8.4 | Measurements record periods and sample sizes; a too-small sample is reported as inconclusive, never as a success. |

### 14.9 — Ten failure-and-recovery scenarios

The M5 exit criterion. Each scenario is an automated test that runs in CI against the local stack and once against staging (`@pytest.mark.ac("FRS-<n>")`). Each one asserts: the failure is detected by a monitor, the right recovery happens (or the right incident or approval is raised), no business action is repeated, the end-of-day report lists it with links, and no finding closes without evidence.

| ID | Failure | Expected recovery |
|---|---|---|
| FRS-1 | Worker killed in the middle of an automation execution | Lease expires; Night Shift restarts it exactly once under its idempotency key |
| FRS-2 | `agent-worker` container replaced during an agent step (rollout) | Lease expires; the step restarts from the task brief; no local state was needed (re-check of tests 2 and 15) |
| FRS-3 | Etsy order intake dead-lettered after the CRM write failed | Reconciliation finds the gap; recovered from the inbox; verified against Etsy (SCN-B) |
| FRS-4 | Claude provider down or returning 429 for a whole night | Monitors, reconciliation, Night Shift and the report run; agent tasks wait with a retry time; the report says Noa's review did not run (test 3 at full scope) |
| FRS-5 | AI budget cap reached during the day | New AI work stops at the cap; safety-critical reserve used only for allowed tasks; automations continue; the owner is notified at 70, 90 and 100% |
| FRS-6 | Poison message blocks a queue | Message dead-lettered after its retries; the queue keeps moving; one incident opened |
| FRS-7 | External write landed but its confirmation was lost (for example tracking pushed to Etsy) | Pre-write re-read finds it; nothing is sent twice (re-check of test 10) |
| FRS-8 | Ledger and payout differ | Ledger-mismatch monitor opens a finding for Roni with both values; nothing is corrected automatically |
| FRS-9 | A state change with no audit record and no author | Missing-audit and authorless-change monitors open a high-severity finding and an incident |
| FRS-10 | A repair outside the allowlist, and an approval that expires while its task waits | An approval is requested; the expired approval is never executed; the task is re-requested or escalated |

| ID | Acceptance criterion |
|---|---|
| AC-14.9.1 | FRS-1 to FRS-10 pass in CI and once on staging; the staging run is recorded with its report. |
| AC-14.9.2 | Running the whole suite twice in a row leaves the same records as running it once (no duplicate incidents, approvals, restarts or ledger entries). |

### 14.10 — Quality Center, Night Shift and Optimization Center screens

Web screens in `htco-clients/apps/web`, built with the design system components and the generated API client:

- **Quality Center** (screen 24): monitor status with last run time, findings list and detail (proof, severity, owner, deadline, verified resolution), reconciliation runs per shop and day, missing events and data-integrity checks.
- **Night Shift**: unfinished work found, resumed jobs, incidents opened, approvals requested, the end-of-day report and its sources.
- **Optimization Center** (screen 25): Omer's pipeline by stage, each proposal with baseline, experiment, PR link, previous actual cost and quality, and realized savings.

| ID | Acceptance criterion |
|---|---|
| AC-14.10.1 | **SCN-A**: for one paid order, the screens answer "did every Etsy order reach the CRM yesterday" and "which automations failed" from records, without reading logs, and show the order's trail across actors. |
| AC-14.10.2 | A monitor that missed its schedule is shown as not run, never as healthy; every figure links to its records. |
| AC-14.10.3 | Each screen has loading, empty, error, permission and offline states, in Hebrew and English, light and dark; buyer PII is masked by role. |

## Test plan

Seeded-condition tests per monitor; model-free tests with the gateway disabled; sampling statistics on fixtures; reconciliation fixtures with seeded differences; crash tests (worker killed mid-execution, mid-step, container replaced); Night Shift idempotency (two runs per night); data-mart formulas tested on fixtures with known answers; lifecycle and policy tests for proposals; automation replay, failure and recovery tests for the three automations; Noa's and Omer's evaluation sets nightly; the FRS-1 to FRS-10 suite in CI and on staging; SCN-A, SCN-B (final), SCN-C and SCN-D; a week of production nightly runs reviewed with the owner; screen tests with recorded fixtures and accessibility checks.

## Security checklist

- [ ] Monitors, Night Shift and rollups run under service identities with read access plus only the writes they need (findings, tasks, incidents, approval requests, mart tables).
- [ ] Noa has no write path to business records other than findings, tasks, incidents and approval requests; Omer has no write path to code, configuration, policies, budgets or deployments (tool bridge tests).
- [ ] Reports, findings, the mart and proposals mask buyer PII by role and never contain model reasoning.
- [ ] Night Shift cannot bypass the approval policy; the allowlist is versioned and reviewed.
- [ ] The [threat model](../../architecture/THREAT_MODEL.md) is updated for the supervision and optimization paths.

## Documentation

`docs/features/supervision/`, `docs/features/night-shift/`, `docs/features/optimization/`, `docs/features/employees/noa.md`, `docs/features/employees/omer.md`; the new automation specification and the full-scope versions of the two scheduling automations in `registry/`; runbook entries for every monitor and every FRS scenario; DATA_MODEL (monitor runs, recovery runs, mart tables), EVENT_CATALOG and PERMISSION_MATRIX updated; COST_MODEL with unit costs and the verified saving; [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios) marks SCN-A to SCN-D done.

## Demo

Kill the worker during an order intake and during an agent step; turn the model gateway off; let Night Shift run; show the restarted execution, the incident, the findings and the end-of-day report marked "Noa review not run"; turn the gateway on and show Noa's review and signature. Show a finding verified on evidence. Show Omer's proposal from detection to pull request to the measured saving, and a second proposal rolled back. Run the FRS suite on staging and show its report.

## Exit gate

- [ ] All AC-14.x pass, including FRS-1 to FRS-10 on staging, SCN-A, SCN-B (final part), SCN-C and SCN-D.
- [ ] Master plan test 5 at full scope and test 6 measurement part pass.
- [ ] Deterministic checks verified to run with no model and no quota.
- [ ] At least one verified saving per unit with no quality regression.
- [ ] Noa and Omer at full scope, Product Metrics Rollup, Quality Review Scheduling and Optimization Review Scheduling are `ready` and approved by the owner.
- [ ] One week of nightly reports reviewed with the owner for false alarms.
- [ ] Milestone **M5 Governance** recorded in [PLAN.md](../PLAN.md) §8.

## Rollback

Noa's and Omer's kill switches (their pilot-scope profile versions can be restored); monitors, Night Shift and rollups disabled by flag (findings and reports remain); every deployed proposal carries its own rollback plan; reconciliation and rollups can be re-run at any time because they only read and record.

## Risks

| Risk | Mitigation |
|---|---|
| Alert fatigue from too many findings | Thresholds tuned from the first week; one finding per condition; severity order in the report |
| Noa's sampling misses a class of errors | Every high-risk operation reviewed; sampling rates revisited monthly from findings |
| Night Shift repeats a business action | Restart only work marked restartable; idempotency key and evidence check before restart; FRS suite in CI |
| Savings that degrade quality | Quality guards in every measurement; rollback by default when a guard regresses |
| Noisy metrics lead to false proposals | Minimum sample sizes; comparable periods; inconclusive is a valid result |
| Data mart drifts from the ledger | Reconciliation test between mart and ledger for every period |

## Cost impact

Monitors, Night Shift, reconciliation, rollups and the mart: database time only, measured per run. Noa and Omer: model usage proportional to exceptions, samples and proposals, recorded in `model_usage` with `billing_mode = API_PRODUCTION`. Expected net saving from verified proposals is recorded at the gate in [COST_MODEL.md](../../cost/COST_MODEL.md).
