# Phase 7 — Tasks, approvals and the policy engine

| | |
|---|---|
| **Milestone** | **M2 — Operational core (MVP-1)** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 6 |
| **Requirements** | FR-APR-01, FR-APR-02, FR-APR-03, FR-APR-04, FR-IAM-06, FR-IAM-07, FR-NTF-01, FR-NTF-03, FR-TASK-01, FR-TASK-02, FR-TASK-03, FR-TASK-04 |
| **Status** | Not started |

## Progress

- [ ] 7.1 Tasks
- [ ] 7.2 Policy engine v1
- [ ] 7.3 Approvals
- [ ] 7.4 Step-up rules for approvals
- [ ] 7.5 Approvals inbox and task views
- [ ] 7.6 Notification preferences

## Goal

The human-in-the-loop core: a task model that every actor uses, a policy engine that decides what each actor may do, and approvals that are safe even under pressure (expired, repeated, self-approved, or approved from a stale screen). With this, HTCO is a usable operational dashboard and ready to host agents.

## Scope

**In:** tasks and task events, policy engine v1 (actors, capabilities, risk, autonomy, versioned policies), approvals with server-side re-checks, step-up re-authentication, approvals inbox and task views in the web app, notification preferences.

**Out:** agents (Phase 8).

## Slices

### 7.1 — Tasks

Task aggregate with the full state set; `task_events`; idempotency key; SLA and owner; API to list and view tasks.

| ID | Acceptance criterion |
|---|---|
| AC-7.1.1 | Every allowed transition is tested; every illegal one from each state is rejected. |
| AC-7.1.2 | Creating a task twice with the same idempotency key returns the first task. |

### 7.2 — Policy engine v1

Actor types (user, worker, agent), capabilities with side-effect and risk levels, autonomy levels, versioned `policies`; decisions `ALLOW`, `REQUIRE_APPROVAL`, `DENY` with reasons; every decision audited with `policy_version`.

| ID | Acceptance criterion |
|---|---|
| AC-7.2.1 | Unknown capability or actor → `DENY`. |
| AC-7.2.2 | A table-driven test covers every row of the permission matrix. |
| AC-7.2.3 | Changing a policy creates a new version; old decisions keep their version. |

### 7.3 — Approvals

Approval aggregate (action, parameters, impact, cost range, risk, evidence, rollback, expiry, requester). First producers: an Operations user's request for a guarded shipment write (decided by the owner) and bulk re-sync requests above 20 orders; agents produce approvals from Phase 8; decide endpoint re-checks role, shop access, MFA level, recent re-authentication for high risk, expiry, policy version and that the decider is not the requester; decisions idempotent; expiry job; events.

| ID | Acceptance criterion |
|---|---|
| AC-7.3.1 | An expired approval cannot be approved and cannot execute. |
| AC-7.3.2 | The requester (user or agent identity) cannot decide its own approval. |
| AC-7.3.3 | Approving twice returns the first decision and executes nothing twice. |
| AC-7.3.4 | A high-risk approval without step-up re-authentication in the last 10 minutes (configurable) returns 403 with a step-up challenge. |
| AC-7.3.5 | The stored decision includes what was shown to the approver (parameters and evidence hash). |

### 7.4 — Step-up rules for approvals

The step-up mechanism from Phase 3 is applied by policy: each capability and risk level declares whether a decision needs `aal2` only or a recent step-up. Biometric step-up on Android and Windows Hello on Windows arrive in Phases 14 and 15, using device keys verified by the server.

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

## Test plan

Domain tests (state machines, policy table), API authorization matrix, concurrency test for double decisions, UI tests; MVP-1 acceptance stories US-01 to US-06 run end to end on the deployed system.

## Security checklist

- [ ] ASVS 2.3.5 (high-value approvals: step-up while there is a single owner; a two-person rule as a policy option once a second Admin exists), 8.3.3 (act with originating permissions), V7 session step-up.
- [ ] Approval execution re-validates everything at execution time, not only at decision time.

## Documentation

`docs/features/tasks/`, `docs/features/approvals/`, `docs/features/policy-engine/`; PERMISSION_MATRIX updated; MVP-1 demo recorded.

## Demo (MVP-1)

The six MVP-1 stories end to end with the real shop: sign-in with MFA, shop capabilities, orders with freshness, a late-tracking incident and email, an approval decided and audited, and a support user blocked from another shop.

## Exit gate — Milestone M2

- [ ] All AC-7.x pass; US-01 to US-06 pass on production; owner signs off MVP-1.

## Rollback

Approvals and tasks are additive; policy versions can be reverted by activating the previous version.

## Risks

| Risk | Mitigation |
|---|---|
| Approval fatigue later when agents arrive | Clear risk levels, batching of low-risk items, pre-approved policies only after evals |

## Cost impact

None.
