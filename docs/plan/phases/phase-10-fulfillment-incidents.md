# Phase 10 — Fulfillment, tracking and incidents

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 8](phase-08-models-tools.md) (router, budgets, cost ledger; needed for Eitan) and [Phase 9](phase-09-etsy-connection.md) (real orders and shipment records) |
| **Requirements** | FR-AUT-05, FR-EML-01, FR-INC-01, FR-INC-02, FR-INC-03, FR-INC-04 (allowlist only), FR-INC-05, FR-INC-06, FR-NTF-01, FR-ORD-03, FR-ORD-05, FR-ORD-06, FR-ORD-07, FR-ORD-09, FR-SUP-04, FR-AGT-09 |
| **Mandatory tests owned** | Test 10 ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)): AutoVA tracking already on Etsy, nothing is sent twice |
| **Status** | Not started |

## Progress

- [ ] 10.1 Tracking detection
- [ ] 10.2 SLA policies and inspector
- [ ] 10.3 Incidents
- [ ] 10.4 Notifications and email
- [ ] 10.5 Incident center and Fulfillment Watch UI
- [ ] 10.6 Guarded shipment write
- [ ] 10.7 AutoVA Fulfillment Watch: registry specification
- [ ] 10.8 AutoVA Fulfillment Watch: implementation
- [ ] 10.9 Fulfillment automations activated
- [ ] 10.10 Eitan activated: fulfillment exception investigation

Slices 10.1 to 10.4 and 10.8 are built as three automations on the engine of [Phase 4](phase-04-durable-core-order-intake.md): **Tracking Synchronization** (10.1), **Incident Detection & Escalation** (10.2 to 10.4) and the new **AutoVA Fulfillment Watch** (10.7, 10.8). Slice 10.9 completes their specifications and activates them. Slice 10.10 activates **Eitan**, the Fulfillment Inspector, on the Claude runtime.

## Goal

HTCO notices fulfillment problems before buyers do. When an order has no tracking after its realistic window, exactly one incident opens and the owner gets exactly one email; when tracking appears in Etsy, the incident closes itself on that evidence. The AutoVA Fulfillment Watch checks that orders AutoVA should be fulfilling are really moving, without ever placing, repeating or redirecting a supplier order and without ever writing tracking that already exists. Routine fulfillment processing is deterministic automations with no model calls. At the end of the phase **Eitan** investigates the exceptions these automations raise, one bounded step at a time, through HTCO tools only.

## Why now

Phase 9 brings real orders and shipment records; Phase 8 brings the model router, budgets and the cost ledger that any employee needs before it runs on real data. Fulfillment is the first place where the business loses money silently (late orders, missing tracking), its inputs are already deterministic and well tested, and Eitan's outputs are internal (incidents, tasks), so he is the safest first employee after the pilot.

## Scope

**In:** tracking detection from shipment records and `order.shipped` webhooks; SLA policies; the inspector job; incidents with state machine and deduplication; in-app notifications; transactional email through Resend from `noreply@office.ht-co.app`; incident center and Fulfillment Watch screens (clients specification screens 6 and 36, first version); guarded shipment write (only if the capability is verified); the new AutoVA Fulfillment Watch automation (registry specification and implementation, read-only, through `AutoVaPort` with the manual adapter until AutoVA offers more); activation of the three automations; Eitan's activation on the Claude runtime; mandatory test 10.

**Out:** supplier quotes and mappings ([Phase 13](phase-13-procurement-finance.md); the watch only reads mappings recorded manually); Noa's nightly supervision over fulfillment ([Phase 14](phase-14-governance.md)); incident remediation by Amir ([Phase 19](phase-19-engineering.md)). Until Phase 19, remediation is either allowlisted, reversible, idempotent code (for example re-running a sync, FR-INC-04) or a person.

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Tracking Synchronization | [`tracking-sync.toml`](../../../registry/automations/tracking-sync.toml) | 10.1, 10.9 | Eitan |
| Incident Detection & Escalation | [`incident-detection-escalation.toml`](../../../registry/automations/incident-detection-escalation.toml) | 10.2, 10.3, 10.4, 10.9 | Amir (the owner is accountable until Amir is active in Phase 19) |
| AutoVA Fulfillment Watch (new) | `autova-fulfillment-watch.toml` (written in slice 10.7) | 10.7, 10.8, 10.9 | Eitan |

The automation activation rule of [Phase 4](phase-04-durable-core-order-intake.md) applies: all 18 specification sections defined, schemas in `packages/contracts/automations/`, replay, failure and recovery tests passing, a runbook, and the owner's approval. Automations keep running when the Claude provider is unavailable or out of quota ([ADR-0017](../../adr/0017-auth-and-billing-policy.md) rule 3); none of them calls a model.

## Employee activated

| Employee | Registry | Work in this phase |
|---|---|---|
| Eitan, Fulfillment Inspector | [`eitan.toml`](../../../registry/employees/eitan.toml) | Investigates fulfillment exceptions, drifts and AutoVA watch findings; opens and updates incidents; creates correction tasks; requests allowlisted automation runs |

## Who handles an incident, by phase

| From | Detection | Investigation | Remediation | Resolution |
|---|---|---|---|---|
| Phase 10 (start) | Incident Detection & Escalation and AutoVA Fulfillment Watch (deterministic) | A person | Allowlisted code or a person | On evidence only (FR-INC-05) |
| Phase 10 (after 10.10) | Same | Eitan for fulfillment exceptions | Allowlisted code or a person; non-allowlisted fixes become approvals | Same |
| Phase 14 | Same, plus Noa's monitors and the nightly supervisor | Eitan; Noa follows up | Same | Same, verified by Noa's follow-up |
| Phase 19 | Same | Same | Amir for technical incidents, within the allowlist; everything else through approvals or engineering requests | Same |

## Owner prerequisites

- DNS for `office.ht-co.app`: SPF, DKIM and DMARC records for Resend (no MX yet).
- Resend account; sending domain verified.
- Agreement on initial SLA windows (from the Phase 0 data and the first live weeks of Phase 9).
- AutoVA answers (A-05, A-06, A-07): does AutoVA write tracking to Etsy for every fulfilled order, does it offer an API, export or webhook, is automatic supplier ordering active for the shop. Without answers the watch runs on Etsy data and manually recorded AutoVA status only.
- Labelled exceptions for Eitan's evaluation set (real-shape cases from Phases 4 and 9 with the expected investigation outcome), reviewed by the owner.
- Approval of Eitan's autonomy level (proposed: L1, acting only inside HTCO) and of the three automations' activation.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. Load `design-system` before any work in `htco-clients` (slice 10.5).
2. Read this file, then [PLAN.md](../PLAN.md), [ADR-0009](../../adr/0009-autova-integration.md), [ADR-0008](../../adr/0008-etsy-integration.md), [ADR-0013](../../adr/0013-automation-engine.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), the Phase 4 file (automation activation rule), the [Phase 6](phase-06-workforce-claude-runtime.md) file (runtime, tool bridge, hooks, evaluation harness), the [Phase 7](phase-07-pilot-office.md) file (employee activation rule as applied to the pilot), `docs/architecture/DATA_MODEL.md`, `EVENT_CATALOG.md`, `PERMISSION_MATRIX.md` and `docs/engineering/TESTING_STRATEGY.md` §10.
3. Refresh the facts at kickoff: Etsy shipment endpoints and their regional restrictions (FR-ORD-07), the AutoVA answers in ASSUMPTIONS.md, Resend limits. Never assume a write permission without a passing capability probe.
4. Work in this order: 10.1 → 10.2 → 10.3 → 10.4 → 10.7 → 10.8 → 10.6 → 10.5 → 10.9 → 10.10. Eitan comes last, after the automations he investigates are active and producing real exceptions.
5. Automations are deterministic code: no model call, no employee tool, no session. Eitan works only as Claude-runtime steps: one session runs one bounded step of one task, starts from the task brief built from Postgres (`business_tasks`, `task_executions.state_summary`), reaches the world only through the in-process `htco` MCP server built from his grants, and ends with a terminal tool (`task.complete`, `task.handoff`, `task.wait_for_approval`, `task.wait_for_event`, `task.escalate`). Waiting for tracking to appear is `task.wait_for_event`, never an open session. Every run is `API_PRODUCTION` and records `billing_mode` on `agent_runs`.
6. Never: write a shipment or tracking number from an automation or an employee; place, repeat or change a supplier order; give Eitan a tool that contacts buyers or writes to Etsy or AutoVA; mark an incident resolved without evidence; activate Eitan before his evaluation passes and the owner approves his autonomy.
7. Stop and ask the owner when SLA windows are unclear, when the capability probe for shipment writes is `unknown`, when AutoVA's answers change what the watch can observe, or when Eitan's evaluation misses a threshold.

## Slices

### 10.1 — Tracking detection

Shipments from ingest (real since Phase 9) become `tracking_events`; the order moves to `SHIPPED` with its tracking code recorded, plus the source and time it was observed; `fulfillment.tracking_detected.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-10.1.1 | A receipt with multiple shipments records each once; repeated observation adds no duplicates. |
| AC-10.1.2 | The order timeline shows when tracking first appeared and from which source (Etsy receipt, `order.shipped` webhook, manual entry). |

### 10.2 — SLA policies and inspector

`sla_policies` per shop, supplier and shipping method, with business-day calendars; defaults from Phase 0 measurements and the first live weeks; scheduled inspector emits `fulfillment.sla_breached.v1` for orders past their window without tracking.

| ID | Acceptance criterion |
|---|---|
| AC-10.2.1 | An order inside its window never produces a breach (boundary tests at exactly the deadline). |
| AC-10.2.2 | Weekends and configured holidays are excluded when the policy says business days. |

### 10.3 — Incidents

Incident aggregate and state machine (FR-INC-02); deduplication (one open incident per resource and cause); automatic transition to `VERIFYING` and `RESOLVED` when evidence arrives; `incidents.opened.v1` and `incidents.resolved.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-10.3.1 | Two breaches for the same order and cause produce one incident. |
| AC-10.3.2 | When tracking appears, the incident resolves with the evidence recorded; without evidence, it never resolves. |
| AC-10.3.3 | Every illegal state transition is rejected (state-machine tests). |
| AC-10.3.4 | A drift recorded by the Phase 4 reconciliation (`sync_drifts`, live since Phase 9) opens one incident linked to the order (FR-ORD-09). |

### 10.4 — Notifications and email

`notifications` (in-app) and `notification_preferences` (from [Phase 5](phase-05-tasks-messages-approvals.md)); `EmailPort` with the Resend adapter; emails sent through the outbox with an idempotency key; templates in Hebrew and English; `Reply-To` set to the support address. Security notifications by email: sign-in from a new device, MFA changes, shop connected or disconnected, integration needs re-authentication, refresh token close to expiry. If Phase 5 already delivered the `EmailPort` for Scheduled Reporting, this slice only adds the incident and security templates.

| ID | Acceptance criterion |
|---|---|
| AC-10.4.1 | One incident produces one email even if the delivery job runs twice. |
| AC-10.4.2 | Email content contains no buyer PII beyond the receipt number. |
| AC-10.4.3 | Resend failures retry with backoff; permanent failures are visible in the admin view. |
| AC-10.4.4 | A sign-in from a new device sends one email to that user; MFA changes and shop connections notify the owner. |

### 10.5 — Incident center and Fulfillment Watch UI

Incident list (severity, shop, age, state), incident detail (evidence, timeline, related order, who did what from activity records), notification bell; Fulfillment Watch screen (SLAs, supplier mapping where recorded, shipment exceptions, inspector findings); links from the order timeline. The full Incident & Debugging Center comes in [Phase 16](phase-16-web-office.md).

| ID | Acceptance criterion |
|---|---|
| AC-10.5.1 | Incident states use status dot plus word, never colour alone. |
| AC-10.5.2 | Screen reader announces new notifications politely, not intrusively. |
| AC-10.5.3 | Eitan's status shown on these screens comes only from recorded task runs (IDLE, QUEUED, RUNNING, WAITING_EXTERNAL, WAITING_APPROVAL, BLOCKED, FAILED, OFFLINE). |

### 10.6 — Guarded shipment write

Only when the capability probe says shipment writes are allowed: an operator confirms a tracking number for an order, the system re-reads the receipt, skips if the code is already there, and otherwise sends through the outbox; 403 shows "capability blocked". This is a human path; no automation and no employee can reach it.

| ID | Acceptance criterion |
|---|---|
| AC-10.6.1 | Without a verified capability, the write path is unreachable (UI hidden and API returns 403). |
| AC-10.6.2 | If the tracking code is already present, no request is sent to Etsy. |
| AC-10.6.3 | Every write requires an MFA session with recent re-authentication and writes an audit record. |
| AC-10.6.4 | The write path is unreachable from every employee session and every automation (tool bridge test over every employee grant; the engine refuses it as a step). |

### 10.7 — AutoVA Fulfillment Watch: registry specification

Add `registry/automations/autova-fulfillment-watch.toml` in the same format as the other automation documents (ADR-0013), first as a draft, then completed in 10.9:

| Field | Value |
|---|---|
| `id` / `name` | `autova-fulfillment-watch` / AutoVA Fulfillment Watch |
| `business_owner` | `eitan` |
| `primary_trigger` | `schedule` (proposed: every 60 minutes per shop); also `event` (`orders.ingested.v1`, `fulfillment.tracking_detected.v1`, `fulfillment.sla_breached.v1`) |
| `callable_from` | `schedule`, `event`, `user_action`, `agent_tool` |
| `side_effect` | `internal` (reads Etsy and AutoVA data; writes only HTCO records) |
| `approval_required` | `false` |
| `ai_steps` | `[]` |
| `activation_phase` | `10` |
| Steps | For each open paid order: read the active supplier mapping (if recorded), the AutoVA status from `AutoVaPort` (manual adapter: the status the owner recorded; API adapter only if AutoVA offers one) and the Etsy shipment records; classify the order as `ON_TRACK`, `AUTOVA_NO_ORDER` (mapping exists but AutoVA shows no supplier order after the grace window), `AUTOVA_NO_TRACKING` (supplier order exists, no tracking on Etsy after the window), `NO_MAPPING` or `UNKNOWN` (no AutoVA data); record an `autova_watch_observations` row; raise a fulfillment exception for every non-`ON_TRACK` class, deduplicated per order and class |
| Never | Places, repeats or changes a supplier order; writes tracking to Etsy or AutoVA; contacts buyers or suppliers |
| Idempotency | Key `autova-watch:{shop_id}:{receipt_id}:{class}`; a replay returns the existing observation |

Add `autova-fulfillment-watch` to Eitan's `allowed_automations` in a new profile version, and update the registry loader tests (`tests/tools/test_registry_files.py`) for the new document.

| ID | Acceptance criterion |
|---|---|
| AC-10.7.1 | The document loads through `htco_application.workforce.registry` with all 18 specification sections present and `readiness = "draft"`; the registry sync creates its `automation_definitions` row with the registry commit. |
| AC-10.7.2 | The specification states the "never" list above, and a test fails if the automation's declared dependencies include any write operation of `AutoVaPort` or `MarketplacePort`. |

### 10.8 — AutoVA Fulfillment Watch: implementation

`AutoVaPort` read operations (supplier mapping status, order status, tracking status) with the manual adapter of [ADR-0009](../../adr/0009-autova-integration.md) (the owner records AutoVA status in the order detail; the same validation and audit as an automated adapter); the automation on the Phase 4 engine; exceptions feed Incident Detection & Escalation and, after 10.10, Eitan's queue.

| ID | Acceptance criterion |
|---|---|
| AC-10.8.1 | With no AutoVA credentials and no manual entries, the watch classifies orders as `UNKNOWN` or from Etsy evidence only, raises no false `AUTOVA_NO_ORDER`, and keeps tracking detection working (ADR-0009 validation). |
| AC-10.8.2 | Replaying the same observation N times through schedule and event paths records one observation and one exception per order and class. |
| AC-10.8.3 | An order whose tracking appears on Etsy moves to `ON_TRACK` and its open exception and incident resolve on that evidence. |
| AC-10.8.4 | The watch makes no call to any write endpoint (strict HTTP mock) and no model call (the gateway records none). |

### 10.9 — Fulfillment automations activated

Tracking Synchronization runs on a schedule and on `order.shipped` events (idempotency key per receipt and shipment); it reads shipment records from Etsy (AutoVA writes tracking there), records tracking codes with the source of proof, and never writes a shipment. Incident Detection & Escalation evaluates SLA rules, opens exactly one incident per root cause and resource, notifies once, and escalates by severity and age. AutoVA Fulfillment Watch runs as specified in 10.7. All three specifications are completed to all 18 sections.

| ID | Acceptance criterion |
|---|---|
| AC-10.9.1 | The three specifications have no `draft` section and pass the registry loader's readiness check. |
| AC-10.9.2 | **Mandatory test 10.** For an order whose tracking AutoVA already wrote to Etsy, replaying the shipment observation N times through Tracking Synchronization (schedule and event paths together), running the AutoVA Fulfillment Watch, and having an operator try the guarded write of 10.6 with the same code results in one tracking event, one transition to `SHIPPED`, zero outbox messages to Etsy and zero requests to Etsy's shipment endpoint (strict HTTP mock), with the skipped write recorded in the activity trail. |
| AC-10.9.3 | Replaying the same breach N times through Incident Detection & Escalation yields one incident, one in-app notification and one email. |
| AC-10.9.4 | When any of the three automations fails until dead-lettered, the failure reaches the owner through the error-monitoring alert rules and the queue health view of Phase 4, never silently. |
| AC-10.9.5 | An escalation step that would need a non-allowlisted remediation creates an approval for a person ([Phase 5](phase-05-tasks-messages-approvals.md)) and runs nothing itself. |
| AC-10.9.6 | With the Claude provider forced to return 429 or switched off, all three automations keep running and opening incidents (master plan test 3 pattern). |

### 10.10 — Eitan activated: fulfillment exception investigation

Eitan investigates the exceptions the automations raise: drifts from Order Deduplication & Verification and reconciliation, fulfillment incidents from Incident Detection & Escalation, AutoVA watch exceptions, and discrepancies between Etsy, AutoVA (when data is available) and the CRM. Each exception becomes a business task assigned to Eitan by the dispatcher of Phase 5. Each step is one Claude-runtime session ([ADR-0016](../../adr/0016-claude-native-agent-runtime.md)): it gathers evidence through read tools (`orders.read`, `activity.query`), triages (rules first, the Decision Advisor only where his policy and the Phase 8 benchmark allow), opens or updates incidents (`incidents.open`), creates correction tasks for a person (`tasks.create`), requests an allowlisted automation run through `automation.trigger` (his `allowed_automations` only), and then ends the step with a terminal tool. When the next evidence is external (tracking expected tomorrow), the step ends with `task.wait_for_event`; the task waits with no session open and the next step starts from the task brief when the event or a delayed job arrives. Routine processing stays with the automations.

Activation follows the employee activation rule of [PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7, as first applied in Phase 7:

1. All 22 sections of Eitan's specification sheet completed from `draft` to `defined` in a reviewed pull request (inputs and outputs as JSON Schemas, triggers, skills, business knowledge, memory, budgets, risk and approvals, evaluation, failure handling, performance metrics), with `runtime = "claude"`.
2. His evaluation set (real-shape exceptions from Phases 4, 9 and this phase with expected investigation outcomes) is built with the Phase 6 harness and passes the thresholds in his `evaluation` section.
3. The owner approves his autonomy level (recorded as an approval).
4. His routing policy is measured on the evaluation set and the first production weeks: cost per inspected order, models used, refusals, advisor use.

| ID | Acceptance criterion |
|---|---|
| AC-10.10.1 | Eitan's sheet has no `draft` section; the registry sync accepts `ready` only with a recorded passing evaluation run. |
| AC-10.10.2 | On the evaluation set, Eitan's findings cite evidence for every conclusion, and he never marks an incident resolved: incidents resolve only on evidence through the 10.3 rules. |
| AC-10.10.3 | Eitan cannot write shipments, contact buyers, call a built-in Claude Code tool, or trigger an automation outside his `allowed_automations`; each attempt is denied by the PreToolUse hook and the tool bridge and recorded in `tool_invocations`. |
| AC-10.10.4 | A step killed mid-way is re-run from the task brief after the lease expires, with no duplicate incident, task or automation trigger (master plan test 2 pattern on a real employee). |
| AC-10.10.5 | Repeated failure on a task escalates to a person with evidence (Maya from Phase 7; Amir for technical causes from Phase 19). |
| AC-10.10.6 | Cost per inspected order and routing outcomes are reported per week from `agent_runs` and `model_usage`, every run has `billing_mode = API_PRODUCTION`, and spend stays within his policy's per-task cap. |
| AC-10.10.7 | An injection payload in a buyer note, receipt message or AutoVA field changes none of Eitan's tools, grants, memory or output schema (fixture corpus from Phase 6). |

## Test plan

Domain tests for SLA math and incident transitions; replay, failure and recovery tests for the three automations on the Postgres engine; AutoVA watch tests with the manual adapter and with no AutoVA data; integration tests for inspector and notifications; email adapter contract tests; mandatory test 10 (AC-10.9.2); Eitan's runtime tests with recorded model responses and the fake gateway, then his evaluation set nightly; injection corpus; UI tests; manual verification with a synthetic order in a test tenant and with a real late order if one occurs.

## Security checklist

- [ ] Email sending only from verified domains; SPF, DKIM, DMARC published (DMARC starting at `p=none`, tightened later).
- [ ] No buyer PII in emails, notifications, model prompts or evaluation datasets beyond identifiers.
- [ ] Shipment write gated by capability, role, MFA and audit; unreachable from employees and automations.
- [ ] AutoVA Fulfillment Watch is read-only by construction (no write operations on its ports).
- [ ] Eitan's session options pass the Phase 6 CI check: `setting_sources=[]`, no built-in tools, never `bypassPermissions`, no secret in `env` but the runtime key.
- [ ] Rule of Two documented for Eitan's step types.

## Documentation

`docs/features/fulfillment-monitoring/`, `docs/features/incidents/`, `docs/features/notifications/`, `docs/features/autova-fulfillment-watch/`, `docs/features/employees/eitan.md`; the three automation specifications completed in `registry/automations/`; ADR-0009 validation log (what the watch can observe without AutoVA access); runbook entries for incident alerts; COST_MODEL with cost per inspected order; ASSUMPTIONS updated with AutoVA answers.

## Demo

Create a synthetic late order in a test shop; watch the incident and email arrive once; add tracking; watch it resolve. Run mandatory test 10. Show an AutoVA watch exception for an order with a recorded mapping and no tracking. Show Eitan investigating it: the task, each step's evidence, the incident update, the correction task, the wait for tracking with no session open, the resume when tracking appears, and the cost of each step.

## Exit gate

- [ ] All AC-10.x pass, including mandatory test 10; SLA windows agreed with the owner; first real-world week reviewed for false alarms.
- [ ] Tracking Synchronization, Incident Detection & Escalation and AutoVA Fulfillment Watch are `ready`, and the owner approved their activation.
- [ ] Eitan is `ready`, his autonomy approved by the owner, and his routing policy measured.

## Rollback

Disable the inspector schedule and the watch by flag or kill switch; incidents and observations remain for review. Eitan's kill switch stops his new sessions within 60 seconds; his open tasks return to the queue for a person.

## Risks

| Risk | Mitigation |
|---|---|
| Too many false alarms | Start with generous windows; tune from data; snooze per order |
| AutoVA data is manual or missing | The watch classifies `UNKNOWN` instead of guessing; tracking from Etsy remains the source of truth |
| Eitan's cost per order grows with long briefs | Summarized `state_summary`, per-task cap, `max_turns`; measured weekly |
| Provider quota runs out | Tasks wait in `WAITING_EXTERNAL`; automations keep detecting and notifying |

## Cost impact

Resend free tier while volume is low. AI cost for Eitan within the owner's cap, measured per inspected order at the gate (no figure assumed before the evaluation run).
