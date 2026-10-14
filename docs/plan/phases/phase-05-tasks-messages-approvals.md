# Phase 5 — Tasks, messages, approvals and activity

| | |
|---|---|
| **Milestone** | **M1 — Foundation** (closes at the end of this phase) |
| **Repositories** | both: `htco-platform` (most work), `htco-clients` (approvals inbox, task, message and activity views) |
| **Size** | L |
| **Depends on** | [Phase 3](phase-03-design-system-web-shell.md), [Phase 4](phase-04-durable-core-order-intake.md) |
| **Requirements** | FR-AGT-07, FR-APR-01, FR-APR-02, FR-APR-03, FR-APR-04, FR-AUD-01, FR-AUD-04, FR-AUD-05, FR-AUT-03, FR-AUT-05, FR-IAM-06, FR-IAM-07, FR-NTF-01, FR-NTF-03, FR-TASK-01, FR-TASK-02, FR-TASK-03, FR-TASK-04 |
| **Status** | Not started |

## Progress

- [ ] 5.1 Event and audit contract
- [ ] 5.2 Activity record store
- [ ] 5.3 Business tasks and task executions
- [ ] 5.4 Task dispatcher
- [ ] 5.5 Agent Task Scheduling & Recovery automation (recovery scheduler)
- [ ] 5.6 Policy engine v1
- [ ] 5.7 Approvals
- [ ] 5.8 Step-up rules for approvals
- [ ] 5.9 Employee messages and task handoff
- [ ] 5.10 Activity query API and activity feed
- [ ] 5.11 Approvals inbox, task, message and activity views (clients)
- [ ] 5.12 Notification preferences
- [ ] 5.13 Scheduled Reporting & Notifications automation

Slices 5.1 and 5.2 come first because every later slice writes records in that shape. Slice 5.5 is built right after the dispatcher (5.4), because the dispatcher's crash test is only complete when the recovery scheduler picks up expired leases.

**Foundation already in code.** `htco_domain.tasks` (`BusinessTask`, `TaskState` with all 13 states of FR-TASK-02 and their allowed transitions), `htco_domain.activity` (`ActivityRecord`, `ActivityKind`, `ActivityOutcome`), `htco_domain.actors` (`ActorKind`, `ActorRef`) and policy engine v0 (`htco_application.access.policy`, tested in `tests/application/test_access_policy.py`). Phase 2 gives the `audit.audit_log` table and the `AuditPort`; Phase 4 gives the `JobQueue` port with claim, lease, heartbeat, retries and dead letter for jobs, the inbox and outbox, and the durable automation engine. This phase adds the task and message tables, the task dispatcher on top of the job queue, the approvals, the activity store and their APIs and screens, and extends the policy engine to employee and automation actors.

## Goal

The human-in-the-loop core that every actor shares, and the contracts the Claude runtime of [Phase 6](phase-06-workforce-claude-runtime.md) plugs into:

- one task model (`business_tasks`, `task_executions`, `task_events`) used by people, employees and automations;
- a **task dispatcher** that runs each step of a task under a lease with heartbeat, retries with backoff, an idempotency key per step and a dead-letter state, plus a recovery scheduler that finds expired leases, so a step that dies in the middle is resumed or re-evaluated without repeating a business action (mandatory test 2 for non-agent steps);
- **employee messages** with a delivery status and an acknowledgement, and a `handoff_task` use case that checks the recipient's permissions and the task's state transition;
- a policy engine that decides for every actor kind, approvals that stay safe under pressure (expired, repeated, self-approved, stale screen), and step-up re-authentication;
- one **activity trail** for every actor, and one field contract shared by audit records, activity records and events: `organization_id`, `shop_id`, `actor_id` (with actor kind), `task_id`, `correlation_id`, `policy_version`, timestamp and `outcome`.

At the end of this phase, Milestone M1 (Foundation) is reached: the platform persists, isolates tenants, replays webhooks safely and runs tasks durably, all proven in CI against Postgres.

## Why now

Phase 4 made the background machinery durable for automations. Employees arrive in Phase 6, and a Claude session is disposable ([ADR-0016](../../adr/0016-claude-native-agent-runtime.md)): everything that must survive it (the task, its state summary, the messages between employees, approvals, costs and evidence) has to live in Postgres first. If the task, message and audit contracts are built after the runtime, the runtime will invent its own state, which is exactly what ADR-0012 rule 3 forbids.

## Scope

**In:**
- The shared event and audit field contract, enforced in code, schemas and tests.
- `activity_records` store and `ActivityPort` writer.
- `business_tasks`, `task_executions` (one row per step attempt, with a versioned `state_summary` field that agent steps fill from Phase 6), `task_events`.
- Task dispatcher: claim with lease, heartbeat, retry with exponential backoff and jitter, idempotency key per step, dead-letter state, safe-restart classification, and the recovery scheduler as the Agent Task Scheduling & Recovery automation.
- `employee_messages` with sender, recipient, task, shop scope, attachments, message kind, delivery status, acknowledgement timestamp and correlation ID; `handoff_task` use case; message API for people.
- Policy engine v1 for actor kinds `user`, `employee`, `automation`, `advisor`, `system`; versioned `policies`.
- Approvals with server-side re-checks at decision and execution time; step-up rules.
- Activity query API and the activity feed; approvals inbox, task board and list, task messages and activity views in the web app.
- Notification preferences; the Scheduled Reporting & Notifications automation.

**Out:**
- AI employees, the employee registry tables, the Claude runtime, the MCP tool bridge, `agent_runs`, `tool_invocations` and kill switches ([Phase 6](phase-06-workforce-claude-runtime.md)). In this phase the dispatcher runs **non-agent steps** (automation steps and fixture step handlers); the agent step handler arrives in Phase 6 through the same dispatcher interface.
- Employees sending messages through a tool (`messages.send_internal`, Phase 6). In this phase messages are sent by people, automations and fixture actors through the API and use cases.
- Routing decisions, model usage and budgets ([Phase 8](phase-08-models-tools.md)).
- The incidents table and lifecycle ([Phase 10](phase-10-fulfillment-incidents.md)). Until then a dead-lettered step raises an admin alert (in-app notification plus an activity record of kind `incident`); see open question in Risks.
- Monitors and Noa's supervision ([Phase 7](phase-07-pilot-office.md) pilot scope, [Phase 14](phase-14-governance.md) full scope).
- The full Office Communication screens ([Phase 16](phase-16-web-office.md)); this phase ships the task-level message thread only.

## Automations activated

| Automation | Registry | Slice | Business owner |
|---|---|---|---|
| Agent Task Scheduling & Recovery | [`agent-task-scheduling-recovery.toml`](../../../registry/automations/agent-task-scheduling-recovery.toml) | 5.5 | Maya (the owner is accountable until Maya is active in Phase 7) |
| Scheduled Reporting & Notifications | [`scheduled-reporting.toml`](../../../registry/automations/scheduled-reporting.toml) | 5.13 | Lia (the owner is accountable until Lia is active in Phase 15) |

The activation rule of [Phase 4](phase-04-durable-core-order-intake.md) applies: all 18 specification sections `defined` (no `draft` left), input and output schemas in `packages/contracts/automations/`, replay, failure and recovery tests passing, a runbook, and the owner's approval ([PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7).

## Owner prerequisites

- The Supabase project and the DigitalOcean account from Phase 0 exist (the database slices of Phases 2 and 4 depend on them; this phase cannot close M1 without them).
- Approve the activation of the two automations above at the end of the phase.
- Confirm the default approval expiry (proposed 24 hours) and the step-up window (proposed 10 minutes).
- Confirm who receives the daily and weekly reports until Lia is active (proposed: the owner, in-app and by email).

## Instructions for the agent

Follow these steps in order. Stop and ask the owner where step 9 says so.

1. Load the skills `software-engineering` (every task), `tdd` (before any production code or migration), `testing`, `feature-verification` (before claiming a slice works) and `feature-docs` (before opening each pull request). For slice 5.11 also load `design-system`.
2. Read this file, then [PLAN.md](../PLAN.md) (§4 steps 4 and 6, §5 test 2, §6 gates), [ADR-0012](../../adr/0012-living-office-organization.md), [ADR-0013](../../adr/0013-automation-engine.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) ("Sessions are disposable; task state is durable" and "Cross-session messaging"), [SOFTWARE_DESIGN §8](../../architecture/SOFTWARE_DESIGN.md#8-jobs-events-and-reliability), [DATA_MODEL](../../architecture/DATA_MODEL.md), [EVENT_CATALOG](../../architecture/EVENT_CATALOG.md), [PERMISSION_MATRIX](../../architecture/PERMISSION_MATRIX.md), [ENGINEERING_RULES](../../engineering/ENGINEERING_RULES.md) (ER-REL, ER-SEC, ER-AI-05, ER-AI-12, ER-AI-16) and [TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md).
3. Read the code that already exists: `htco_domain.tasks`, `htco_domain.activity`, `htco_domain.actors`, `htco_application.access.policy`, `htco_application.automation.engine`, the Phase 4 `JobQueue` port and its Postgres adapter, and the Phase 2 `AuditPort`. Extend them; do not write parallel models.
4. Build the slices in the order of the Progress list. One slice, one pull request. For each: acceptance criteria as failing tests first, then code, then verify against the local Supabase stack, then docs.
5. Every migration is additive, has RLS from its first version, and has pgTAP tests for organization and shop isolation in the same pull request.
6. Every state-changing use case writes its activity record and, where FR-AUD-01 applies, its audit record, using the field contract of slice 5.1. The coverage test of AC-5.2.1 must stay green.
7. Contract first: every new route goes into `packages/contracts/openapi.yaml` and every new event into `packages/contracts/events/` and EVENT_CATALOG before it is implemented.
8. Never in this phase: call a model or the Claude Agent SDK; add employee-specific code paths to the dispatcher (agent steps arrive in Phase 6 through the generic step-handler interface); let a message, a conversation or a model output complete a task; let an employee or automation decide an approval; put prompts, model output or raw external payloads in any activity, message or audit column; trust an `organization_id`, `shop_id` or actor sent by a client or carried inside a message body.
9. Stop and ask the owner when: a policy table row in PERMISSION_MATRIX is ambiguous; an automation specification needs a business decision (report contents, recipients, retry limits); the job-queue adapter from Phase 4 cannot give the lease guarantees in slice 5.4 without a change to ADR-0007; or a slice would need a table or event name that conflicts with DATA_MODEL or EVENT_CATALOG.
10. At the end, run the M1 exit gate checklist below on the deployed development environment, record evidence in the PLAN status table, and prepare the demo.

## Slices

### 5.1 — Event and audit contract

One field contract shared by the event envelope ([SOFTWARE_DESIGN §8.4](../../architecture/SOFTWARE_DESIGN.md#84-event-envelope)), `audit.audit_log` (Phase 2), `activity_records` (5.2), `task_events`, `employee_messages` and, from Phase 6, `agent_runs` and `tool_invocations`. The fields, from the master plan: `organization_id`, `shop_id`, `actor_id` with actor kind (an `ActorRef`), `task_id`, `correlation_id`, `policy_version`, timestamp (`occurred_at`, timezone-aware UTC) and `outcome`, plus `causation_id`. A field may be null only where the domain says it does not apply (an organization-level record has no shop; a record outside any task has no task; a record with no policy decision has no policy version), and each record type declares which fields are mandatory. The contract is a pure type in `htco_domain` (`RecordContext`), a JSON Schema fragment reused by all event schemas, and a Postgres column set with check constraints.

| ID | Acceptance criterion |
|---|---|
| AC-5.1.1 | `RecordContext` refuses a naive timestamp, a blank or unknown actor kind, a missing `correlation_id`, and an `outcome` outside `succeeded`, `failed`, `denied`, `pending` (domain tests). |
| AC-5.1.2 | Every event schema in `packages/contracts/events/` includes the shared fragment; a CI check fails when a schema omits it. |
| AC-5.1.3 | Any record that results from a policy decision (approvals, denials, tool decisions later) has a non-null `policy_version`; a check constraint and a test enforce it per table. |
| AC-5.1.4 | `organization_id`, `shop_id` and the actor are always taken from the authenticated context, the task or the automation's service identity, never from a request body or message content (API tests send forged values and the stored record keeps the server's values). |
| AC-5.1.5 | One `correlation_id` links an order event, the task it creates, the task's executions, messages, approvals and activity records; a test follows one chain across all of them. |

### 5.2 — Activity record store

`activity_records` table for `ActivityRecord`: kind (`ActivityKind`: task execution, automation execution, business event, tool invocation, message, decision, approval, incident, routing decision, engineering change, reconciliation, quality finding), the slice 5.1 fields, action, a short business summary, causation ID. Append-only grants, RLS by organization and shop, partitioned by month. An `ActivityPort` writer is called by the use cases that already write audit or events: task transitions, task executions, messages, approvals, automation executions (the Phase 4 automations start writing here), notifications. The summary is a business statement built from a code template ("Retried step 2 of task 'Weekly report': lease expired after 5 minutes"), never model reasoning, prompts or raw payloads.

| ID | Acceptance criterion |
|---|---|
| AC-5.2.1 | Every task transition, task execution outcome, message delivery change, approval decision and automation execution writes exactly one activity record with the slice 5.1 fields (a coverage test walks every state-changing use case and fails when one writes none or two). |
| AC-5.2.2 | The application role cannot update or delete activity records; a summary over the maximum length, without a correlation ID or with a naive timestamp is rejected. |
| AC-5.2.3 | A record of organization A is invisible to organization B, and a shop-scoped record is invisible to a user without that shop (pgTAP). |
| AC-5.2.4 | No column of `activity_records` can hold a prompt, model output or raw external payload: summaries come only from code templates, and a test fails if a writer passes free text from a model or an external source. |

### 5.3 — Business tasks and task executions

Tables `business_tasks` (the `BusinessTask` aggregate: organization, shop, title, assignee as an `ActorRef`, state, queue, priority, SLA due time, owner, idempotency key, correlation ID, input references, evidence references, parent task), `task_executions` and `task_events` (one row per transition, with actor, reason and evidence reference).

`task_executions` holds one row per attempt of one step of a task: task ID, step number, attempt number, handler kind (`automation`, `employee`, `user`, `fixture`), handler reference, status (`claimed`, `running`, `succeeded`, `failed`, `retry_scheduled`, `dead_lettered`, `cancelled`), lease owner, lease expiry, last heartbeat, step idempotency key, next attempt time, last error (typed code and safe message), started and finished times, and **`state_summary`**: a versioned JSON document (`state_summary_schema` names its schema, `state_summary_version` increases by one on every write) that carries a step's summarized working state to the next step. Non-agent steps may leave it empty; agent steps fill it from Phase 6, where a session starts from the task brief built from it. A step may only write a `state_summary_version` exactly one above the version it read (optimistic concurrency), so two attempts can never both commit their state.

API: list and view tasks with filters (state, assignee, queue, shop, SLA breach), create a task (people and automations), cancel, and view one task's executions and events.

| ID | Acceptance criterion |
|---|---|
| AC-5.3.1 | Every allowed transition of `TaskState` is tested against Postgres; every illegal one from each state is rejected and writes nothing. |
| AC-5.3.2 | Creating a task twice with the same idempotency key returns the first task, with no second row, event or activity record. |
| AC-5.3.3 | A task reaches `COMPLETED` only through a recorded transition that references evidence (a resource, a succeeded execution or a decided approval); no message or conversation text can complete a task. |
| AC-5.3.4 | A write of `state_summary` whose version is not exactly the stored version plus one is rejected; the stored summary validates against its declared schema; a summary larger than the configured limit is refused. |
| AC-5.3.5 | `state_summary` rejects secrets and buyer PII patterns (the same detector as evaluation datasets) and is covered by RLS and the retention policy of the task. |
| AC-5.3.6 | Tasks, executions and events of organization A or of a shop the user is not granted are invisible (pgTAP and API tests return 404). |

### 5.4 — Task dispatcher

The dispatcher turns a `QUEUED` task into one step run at a time, on top of the Phase 4 job queue. It is generic: a **step handler** registry maps a handler kind to code (`automation` runs an automation step, `fixture` runs test handlers; `employee` is registered by the Claude runtime in Phase 6). Behaviour:

- **Claim with lease.** An atomic claim moves the task `QUEUED → CLAIMED`, creates a `task_executions` row with a lease owner and lease expiry, and only then `CLAIMED → RUNNING`. Two dispatchers can never hold the same task.
- **Heartbeat.** The running handler extends the lease at a configured interval; a missed heartbeat lets the lease expire.
- **Idempotency key per step.** `task:{task_id}:step:{n}` (attempt number excluded) is passed to every side effect of the step; external effects go through the outbox with that key, so a retried attempt finds the earlier effect instead of repeating it.
- **Safe-restart classification.** Each step declares whether it is safely restartable (no side effect before its final commit, or all side effects keyed) or must be re-evaluated (a side effect may have happened: check the evidence first).
- **Retry with backoff.** A typed retryable failure moves the task to `RETRY_SCHEDULED` with exponential backoff and jitter from the handler's retry policy (automation retry tables from the registry).
- **Dead letter.** After the maximum attempts, or on a non-retryable error, the execution is `dead_lettered`, the task moves to `FAILED` (then `ESCALATED` by the recovery scheduler), and an admin alert is raised.
- **Graceful shutdown.** On SIGTERM the dispatcher stops claiming, lets the running step finish within a deadline, and otherwise leaves the lease to expire.

The dispatcher writes queue age, attempts, lease expiries and dead letters as metrics, and an activity record per execution outcome.

| ID | Acceptance criterion |
|---|---|
| AC-5.4.1 | Two dispatcher instances racing for the same queued task produce exactly one `CLAIMED` transition and one `task_executions` row (concurrency test against Postgres, repeated 100 times). |
| AC-5.4.2 | A handler that stops heartbeating loses its lease after the configured expiry; a late write from that attempt (state summary, transition or outbox entry) is rejected because its lease is no longer current (fencing by execution ID and lease token). |
| AC-5.4.3 | A retryable failure schedules the next attempt with exponential backoff and jitter inside the configured bounds; after the maximum attempts the execution is `dead_lettered`, the task is `FAILED`, and one admin alert and one activity record are written. |
| AC-5.4.4 | Every side effect of a step carries the step's idempotency key; a retried attempt that reaches the same side effect produces no second effect (outbox uniqueness test). |
| AC-5.4.5 | SIGTERM during a step lets it finish within the deadline; past the deadline the lease is left to expire and the step is recovered by slice 5.5, never run twice in parallel. |
| AC-5.4.6 | **Mandatory test 2, non-agent steps (part 1).** A fixture step is killed (the worker process is terminated) after its first keyed side effect and before its final commit. The lease expires, the recovery scheduler (5.5) re-queues the task, the next attempt runs, the side effect exists exactly once and the task completes; a restartable step resumes, and a re-evaluate step checks the evidence and completes without repeating the action. Automated kill test in CI. |
| AC-5.4.7 | The step-handler interface carries the task, the execution, the step idempotency key, the latest `state_summary` and a lease handle, and imports nothing from any agent framework or SDK (import-linter). |

### 5.5 — Agent Task Scheduling & Recovery automation (recovery scheduler)

The deterministic automation that keeps work moving, run on a schedule by the Phase 4 engine with no model calls. It scans for: executions whose lease expired; tasks `RUNNING` with no live execution; tasks past their SLA; tasks waiting on a resource or approval that already arrived; tasks in `RETRY_SCHEDULED` whose time came. For each, by the step's safe-restart classification, it re-queues restartable work, re-queues re-evaluate work with a "check evidence first" flag, closes tasks whose business effect is already recorded, and escalates the rest to a person. It never repeats a completed business action. From Phase 6 it also covers agent steps, through the same tables.

| ID | Acceptance criterion |
|---|---|
| AC-5.5.1 | A task whose lease expired is re-queued exactly once if restartable, and escalated to a person if not; both outcomes write an activity record with the slice 5.1 fields. |
| AC-5.5.2 | A task whose business effect is already recorded (for example the outbox entry was delivered) is completed with that evidence and not run again. |
| AC-5.5.3 | Two scheduler instances never recover the same task twice (lock test). |
| AC-5.5.4 | A task waiting for an approval that has been decided, or for an event that has arrived, is re-queued within one scheduler period. |
| AC-5.5.5 | **Mandatory test 2, non-agent steps (part 2).** Together with AC-5.4.6: the crash is noticed by this scheduler, not by a person, within one lease period plus one scheduler period. |
| AC-5.5.6 | The specification has all 18 sections defined; replay, failure and recovery tests pass; the runbook exists. |

### 5.6 — Policy engine v1

Actor kinds from `ActorRef` (`user`, `employee`, `automation`, `advisor`, `system`), capabilities with side-effect and risk levels, autonomy levels, versioned `policies`; decisions `ALLOW`, `REQUIRE_APPROVAL`, `DENY` with reasons; every decision audited with `policy_version`. The engine is the single authority used by the API, the approval use cases, the dispatcher, the message and handoff use cases, and from Phase 6 the MCP tool bridge.

| ID | Acceptance criterion |
|---|---|
| AC-5.6.1 | Unknown capability or actor → `DENY`. |
| AC-5.6.2 | A table-driven test covers every row of the permission matrix. |
| AC-5.6.3 | Changing a policy creates a new version; old decisions keep their version. |
| AC-5.6.4 | Employee and automation actors are evaluated with their own permission sets, never a user's (FR-IAM-07): an automation acts only under its service identity, and an employee's request runs with the employee's grants and the originating user's shop access, whichever is narrower. |
| AC-5.6.5 | An `advisor` actor has no capabilities: any action it requests is `DENY`, so a Decision Advisor answer can never authorize anything ([ADR-0015](../../adr/0015-decision-advisor-and-routing.md)). |
| AC-5.6.6 | Capabilities that the policy marks as production-changing (deploy, merge, push to a protected branch, permission change) are `DENY` for every `employee` and `automation` actor, whatever the autonomy level; Phase 6 reuses this row for mandatory test 9. |

### 5.7 — Approvals

Approval aggregate (action, parameters, impact, cost range, risk, evidence, rollback, expiry, requester as an `ActorRef`, task, `policy_version`). First producers in this phase: a manual re-run of a dead-lettered execution whose step has external side effects (requested by an Operations user or Admin, decided by the Owner or another Admin), and bulk re-queue requests above 20 tasks. Employees produce approvals from Phase 6 (`task.wait_for_approval`); the first real ones come from the pilot in Phase 7. The decide endpoint re-checks role, shop access, MFA level, recent re-authentication for high risk, expiry, policy version and that the decider is not the requester; decisions are idempotent; an expiry job; events. A decided approval re-queues its waiting task through the dispatcher (5.4); execution re-validates everything at execution time.

| ID | Acceptance criterion |
|---|---|
| AC-5.7.1 | An expired approval cannot be approved and cannot execute. |
| AC-5.7.2 | The requester (a user, employee or automation identity) cannot decide its own approval, and no employee, automation or advisor can decide any approval. |
| AC-5.7.3 | Approving twice returns the first decision and executes nothing twice. |
| AC-5.7.4 | A high-risk approval without step-up re-authentication in the configured window returns 403 with a step-up challenge. |
| AC-5.7.5 | The stored decision includes what was shown to the approver (parameters and evidence hash) and the `policy_version`; executing with parameters that differ from the hash is refused. |
| AC-5.7.6 | A task in `WAITING_FOR_APPROVAL` holds no lease and no running process while it waits; deciding the approval re-queues it, and rejecting it moves it to the state its specification names (cancelled or escalated). |

### 5.8 — Step-up rules for approvals

The step-up mechanism from Phase 3 is applied by policy: each capability and risk level declares whether a decision needs `aal2` only or a recent step-up. Biometric step-up on Android and Windows Hello on Windows arrive in [Phase 17](phase-17-android.md) and [Phase 18](phase-18-windows.md), using device keys verified by the server.

| ID | Acceptance criterion |
|---|---|
| AC-5.8.1 | Changing a capability's step-up rule creates a new policy version, and each decision records the rule that applied. |

### 5.9 — Employee messages and task handoff

`employee_messages` is the only channel between employees, people and departments inside HTCO ([ADR-0016](../../adr/0016-claude-native-agent-runtime.md) "Cross-session messaging"); Claude Code's own cross-session messaging is never used for business communication. Columns: sender and recipient (each an `ActorRef`; a recipient may also be a department), `task_id`, `shop_scope` (the shop the message is about, or none for organization-level), attachments (resource references only: task, order, listing, file in Storage; never raw content), `message_kind` (`handoff`, `request`, `reply`, `notice`, `escalation`), a business summary with a length limit, `delivery_status` (`pending`, `delivered`, `acknowledged`, `failed`, `expired`), `ack_timestamp`, `correlation_id`, `causation_id`, idempotency key, plus the slice 5.1 fields. Delivery means the recipient's queue or inbox has the message; acknowledgement means the recipient's handler (a person, an automation, or an employee step from Phase 6) recorded that it took it. Unacknowledged messages are retried, then marked `expired` and escalated to the sender's manager or the owner.

`handoff_task` is one use case, in one transaction: it checks that the sender may hand off this task, that the recipient may receive it (the policy engine evaluates the recipient's grants for the task's capability and the task's shop; an employee recipient must be allowed to act on that shop), that the task's state transition is allowed, then reassigns or creates the receiving task, moves the task to `QUEUED` for the recipient, and writes one `handoff` message, one task event and one activity record, all with one correlation ID. A message never carries authority: what the recipient may do is decided by its own grants, never by the message text.

API for people: send a message on a task, list a task's thread, list my inbox (people), acknowledge.

| ID | Acceptance criterion |
|---|---|
| AC-5.9.1 | A message is stored before it counts as sent; sending twice with the same idempotency key stores one message. |
| AC-5.9.2 | `delivery_status` moves only `pending → delivered → acknowledged`, or to `failed` or `expired`; `ack_timestamp` is set exactly when it becomes `acknowledged`, and only the recipient (or its handler) can acknowledge. |
| AC-5.9.3 | An unacknowledged message is retried with backoff and then marked `expired`, with one escalation and one activity record. |
| AC-5.9.4 | `handoff_task` to a recipient without the permission for the task's capability or shop is refused with a reason and changes nothing; a handoff from a state that does not allow it is refused the same way. |
| AC-5.9.5 | A message whose `shop_scope` or attachments refer to a shop the recipient is not granted is refused; a recipient never reads a message or attachment from another organization or an ungranted shop (pgTAP and API tests). This is the storage half of mandatory test 4; the employee half is in Phase 6. |
| AC-5.9.6 | A message body that asks the recipient to do something outside its grants changes no permission: the request is evaluated against the recipient's own grants (policy test with a fixture recipient). |
| AC-5.9.7 | Every message appears in its task's activity trail; summaries are limited in length and attachments are references, never inline content. |

### 5.10 — Activity query API and activity feed

`GET /v1/activity` with filters by actor (`employee:shon`, `automation:etsy-order-intake`), actor kind, department, shop, task, kind, outcome and time range, cursor-paginated; `GET /v1/tasks/{task_id}/activity` for one task's trail (by correlation and causation). A web activity feed with the same filters and saved queries such as "what did an actor do today" and "which automations failed this week". A realtime update for new records the user may see (Supabase Realtime over RLS, or polling; decided at kickoff).

| ID | Acceptance criterion |
|---|---|
| AC-5.10.1 | "What did `automation:etsy-order-intake` do today" returns exactly that day's records for that actor, in the user's time zone, and nothing from shops the user is not granted. |
| AC-5.10.2 | "Which automations failed" returns every execution with outcome `failed` in the range, each linked to its execution and dead-letter state. |
| AC-5.10.3 | A task's trail shows every record linked by correlation ID in causal order, across actors, including messages, executions and approvals. |
| AC-5.10.4 | The feed states its data source and freshness; an empty result says "no recorded activity", never an inferred state. |
| AC-5.10.5 | A realtime update never delivers a record the user could not fetch through the API (RLS test on the realtime channel or the polling endpoint). |

### 5.11 — Approvals inbox, task, message and activity views (clients)

In `htco-clients/apps/web`, on the design system: approvals inbox with approval cards (action, impact, cost range, risk, evidence, diff, rollback, expiry countdown); task board by state and task list with SLA; task detail with its executions (attempts, lease, last error, dead-letter state), its message thread with delivery and acknowledgement state, and its activity trail; the activity feed of 5.10; audit popover on decisions. Generated API client from `packages/api-clients`.

| ID | Acceptance criterion |
|---|---|
| AC-5.11.1 | A screen reader reads the approval card's action, reason, impact and risk in order. |
| AC-5.11.2 | Approve and reject buttons are disabled with an explanation when the approval is expired or the user lacks permission. |
| AC-5.11.3 | The card shows server time to expiry, not client time. |
| AC-5.11.4 | Task and message states shown are exactly the stored states (no client-side inference); a dead-lettered execution shows its last error code and the "request re-run" action only for roles allowed to request it. |
| AC-5.11.5 | Hebrew (RTL) and English, light and dark, pass the accessibility checks of the design system. |

### 5.12 — Notification preferences

Per severity and per shop; applied to in-app and email notifications. Notifications in this phase: approvals waiting, approvals expiring, dead-lettered executions, tasks past their SLA, unacknowledged messages escalated.

| ID | Acceptance criterion |
|---|---|
| AC-5.12.1 | Muting a shop stops its non-critical notifications; critical security notifications cannot be muted. |

### 5.13 — Scheduled Reporting & Notifications automation

Builds the daily and weekly operations summary from recorded activity only: orders ingested (from Phase 4, fixtures until Phase 9), tasks created, completed, failed and dead-lettered, approvals pending and expired, messages unacknowledged, stale shops. Delivered as an in-app notification and by email according to preferences (5.12), through the same transactional email path as Phase 2's invitations (verify at kickoff). Lia presents these reports from Phase 15; until then they go straight to the owner.

| ID | Acceptance criterion |
|---|---|
| AC-5.13.1 | Every number in a report links to the activity records or rows it was computed from; a section whose source is unavailable says so instead of showing zero. |
| AC-5.13.2 | Running the daily report twice for the same day sends one report (idempotency key per organization, report type and period). |
| AC-5.13.3 | The specification has all 18 sections defined; replay, failure and recovery tests pass; the runbook exists. |

## Mandatory tests owned by this phase

| Test ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | Where |
|---|---|
| 2 — a task dies mid-step: the lease expires, the dispatcher notices, the step resumes or is re-evaluated with no duplicate action (**non-agent steps**) | AC-5.4.6, AC-5.5.5. Phase 6 repeats it for agent steps (POC-2). |

This phase also builds the storage and policy half of test 4 (AC-5.9.5, AC-5.9.6); the test itself is owned by Phases 6 and 7.

## Test plan

- Domain tests: task state machine, execution status machine, message delivery state machine, `RecordContext`, policy table with all five actor kinds.
- Postgres integration tests (local Supabase stack in CI): claim races, lease expiry and fencing, backoff timing with a controllable clock, dead letter, outbox uniqueness, `state_summary` optimistic concurrency, approval double-decision concurrency.
- Kill tests: terminate the worker process mid-step (AC-5.4.6) and during graceful shutdown (AC-5.4.5).
- pgTAP: RLS for `business_tasks`, `task_executions`, `task_events`, `employee_messages`, `approvals`, `policies`, `activity_records`; append-only grants; check constraints of the slice 5.1 contract.
- API authorization matrix over every new route; Schemathesis on the contract.
- Activity coverage test over every state-changing use case (AC-5.2.1).
- Replay and recovery tests for both automations.
- Web UI: component tests, accessibility checks, end-to-end tests for approve, reject, expired approval, task detail and the activity feed.
- **M1 regression suite** (see the exit gate): webhook replay (Phase 4), persistence and RLS (Phases 2, 4, 5) in one CI job against Postgres.

## Security checklist

- [ ] ASVS 2.3.5 (high-value approvals: step-up while there is a single owner; a two-person rule as a policy option once a second Admin exists), 8.3.3 (act with originating permissions), V7 session step-up.
- [ ] Approval execution re-validates everything at execution time, not only at decision time.
- [ ] Tenant, shop and actor in every record come from the server context (AC-5.1.4); forged values in requests and messages are ignored.
- [ ] Messages carry references, not content; no message grants authority (AC-5.9.6).
- [ ] Lease fencing prevents a zombie attempt from writing (AC-5.4.2).
- [ ] `state_summary` and messages scanned for secrets and buyer PII.
- [ ] [Threat model](../../architecture/THREAT_MODEL.md) reviewed for the dispatcher (replay, duplicate dispatch, starvation) and messages (spoofing, cross-shop leak).

## Documentation

`docs/features/tasks/` (tasks, executions, dispatcher, recovery), `docs/features/messages/`, `docs/features/approvals/`, `docs/features/policy-engine/`, `docs/features/activity-records/`; DATA_MODEL tables marked created (note the names `business_tasks`, `task_executions`, `employee_messages`); EVENT_CATALOG entries for task, message and approval events (including `agents.message_sent.v1` and an acknowledgement event); SOFTWARE_DESIGN §8 updated with the dispatcher; the two automation specifications completed in `registry/automations/` with runbooks; PERMISSION_MATRIX updated; [TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md) mandatory test 2 status.

## Demo (Milestone M1)

On the deployed development environment:

1. Replay a recorded, signed Etsy webhook three times and show one order (Phase 4), with its correlation ID.
2. Create a task from that order, run a fixture step, kill the worker mid-step, and show the lease expiring, the recovery scheduler re-queuing it, and one side effect in the outbox.
3. Exhaust a step's retries and show the dead letter, the admin alert, and a re-run request that needs an approval; decide it with step-up.
4. Hand a task from one fixture actor to another; show the message moving `pending → delivered → acknowledged`, and a handoff refused because the recipient lacks the shop.
5. Show the activity feed answering "what did Agent Task Scheduling & Recovery do today" and "which automations failed this week", and the first daily operations report.
6. Show a second test tenant and a Support user without the shop failing to see any of it, through the API and directly in SQL.

## Exit gate — Milestone M1 (Foundation)

- [ ] All AC-5.x pass in CI, including mandatory test 2 for non-agent steps.
- [ ] **M1 regression suite passes in one CI run against Postgres:** webhook replay and duplicate-webhook tests (Phase 4, mandatory test 1 with fixtures), persistence and restart tests (Phases 4 and 5), and the RLS and isolation tests of Phases 2, 4 and 5 (pgTAP and API).
- [ ] The first restore drill of Phase 4 (mandatory test 13) is recorded.
- [ ] Agent Task Scheduling & Recovery and Scheduled Reporting & Notifications are `ready`, and the owner approved their activation.
- [ ] Demo run on the deployed environment; owner signs off M1.

## Rollback

Tasks, executions, messages, approvals and activity tables are additive. The dispatcher sits behind a feature flag: switched off, queued tasks stay queued and nothing is lost. Policy versions can be reverted by activating the previous version. The two automations can be switched off through the automation engine.

## Risks

| Risk | Mitigation |
|---|---|
| Lease timing wrong for long steps (false expiry, double run) | Heartbeat interval well under the lease; fencing tokens (AC-5.4.2); lease per handler kind, measured in the kill tests |
| The dispatcher duplicates the Phase 4 job queue | The dispatcher uses the job queue for wake-ups and its own `task_executions` rows for step state; one ADR-0007 update records the split |
| Dead letters with no incident system until Phase 10 | Admin alert plus an activity record of kind `incident` now; Phase 10 converts them into incidents (open question for the owner: build a minimal `incidents` table earlier?) |
| Approval fatigue when employees arrive | Clear risk levels, batching of low-risk items, pre-approved policies only after evaluations |
| The activity table grows fast | Monthly partitions, retention per data class, indexes on actor, shop and correlation ID |
| `state_summary` becomes a hidden memory store | Schema per step, size limit, PII and secret scan, owned by the task's retention policy |

## Cost impact

No model spend. Database growth from activity and execution rows; within the Supabase plan in [COST_MODEL.md](../../cost/COST_MODEL.md). The `agent-worker` runs the dispatcher with no change in size.
