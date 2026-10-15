# Phase 15 — The office and the executive team

| | |
|---|---|
| **Milestone** | **M5 — The office** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 13](phase-13-suppliers-finance.md) and [Phase 14](phase-14-email-support.md) |
| **Requirements** | FR-ADM-04, FR-AGT-08, FR-AGT-09, FR-AUD-05, FR-EML-04, FR-NTF-02, FR-OFC-01, FR-OFC-02, FR-OFC-03, FR-OFC-04, FR-OFC-05, FR-OFC-06, FR-OFC-07, FR-OFC-08 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 15.1 Live-update spike and ADR
- [ ] 15.2 Employee states from recorded activity
- [ ] 15.3 Executive Office
- [ ] 15.4 Employee Departments and Employee Profile
- [ ] 15.5 Automation Center and AI Tools Center
- [ ] 15.6 Decision & Model Observatory
- [ ] 15.7 Engineering Console (read-only)
- [ ] 15.8 Office Communication
- [ ] 15.9 Activity Feed & Audit, Notifications & Approvals
- [ ] 15.10 Maya and Lia activated; daily and weekly office reviews

## Goal

The owner sees the AI office as it really is and works with it: the Executive Office, the employee departments and profiles, the Automation Center, the AI Tools Center, the Engineering Console (read-only until [Phase 19](phase-19-engineering.md)), Office Communication, the Decision & Model Observatory, the Activity Feed & Audit, and Notifications & Approvals. Every state shown comes from recorded activity (tasks, sessions, executions, invocations, decisions, findings); nothing is inferred or fabricated, and the office is read-only when the agent worker is offline. **Maya** (General Manager) plans, distributes and coordinates work and runs the daily and weekly office reviews; **Lia** (Owner Assistant) is the owner's conversation partner, delivering reports and presenting approvals.

## Why now

By this phase nine employees (Eitan, Noa, Daniel, Noam, Shon, Nika, Tom, Roni, Dana) and fourteen of the fifteen automations are active: there is real activity to show, and enough coordination work to need a manager. Building the office earlier would have meant showing empty or invented states.

## Scope

**The home overview (owner, 2026-10-15).** The web app's home screen grows into the business's cockpit, and its space must be used fully:
- key numbers: orders, revenue, profit, average order, late shipments, review average, out of stock;
- trends over time;
- important messages and decisions that pop up there;
- the office live: who is working on what right now, with their latest activity.

Each block lands with the phase that brings its data (Phases 5–14). This phase completes the layout and the live office view. Until then the overview shows only real data and says what will appear.


**In:** live-update transport (decided by a spike: server-sent events with reconnects through DigitalOcean's request timeout, or Supabase Realtime private channels, A-12); employee states `IDLE`, `RUNNING`, `WAITING`, `NEEDS_APPROVAL`, `BLOCKED`, `OFFLINE` derived only from recorded activity and heartbeats; the office areas listed in the goal, each with an accessible list view; Office Communication (conversations with Lia, Maya, one employee or a department; internal employee-to-employee messages; task threads and handoff history; search; approval cards inline); web push with consent; optional Hermes channel through the Action API (D-10); per-employee email aliases only after abuse tests (FR-EML-04); Maya's and Lia's activation; daily and weekly office reviews.

**Out:** the animated office canvas (a later iteration once the list and grid views prove useful); write actions in the Engineering Console ([Phase 19](phase-19-engineering.md)); Omer's proposals (the Executive Office shows the area empty until [Phase 18](phase-18-optimization.md)).

## Owner prerequisites

- D-10 decided (Hermes channel or not).
- Approval of Maya's and Lia's autonomy levels (proposed L1 both).
- Agreement on the daily and weekly review format and times.

## Employees activated

| Employee | Registry | Work |
|---|---|---|
| Maya, General Manager | [`maya.toml`](../../../registry/employees/maya.toml) | Receives objectives from the owner through Lia; plans and distributes tasks; coordinates departments; escalates; runs the daily and weekly office reviews; prioritizes Omer's proposals from Phase 18 |
| Lia, Owner Assistant | [`lia.toml`](../../../registry/employees/lia.toml) | Conversations with the owner; reports, explanations and alerts; presents approvals with impact and risk; opens and tracks tasks for the owner |

Each follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7. Maya and Lia also become the business owners of Agent Task Scheduling & Recovery and Scheduled Reporting & Notifications (active since [Phase 7](phase-07-tasks-approvals.md)).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 15.1 | Live-update spike and ADR | Reconnect and replay with a cursor; no state derived only from the stream |
| 15.2 | Employee states from recorded activity; offline detection from heartbeats | Worker offline → office read-only, no employee shown as running; a `draft` employee is never shown as working |
| 15.3 | Executive Office: manager overview, Noa's findings, Omer's proposals, approvals, business health | Every figure links to its records |
| 15.4 | Employee Departments and Employee Profile: name, role, goals, activity, tools, skills, permissions, automations, sessions, memory summaries, model policy, evaluations, costs | The profile shows the specification version in use and its evaluation result |
| 15.5 | Automation Center (definitions, triggers, executions, failures, retries, metrics, authorized manual runs) and AI Tools Center (tools, versions, status, capabilities, access, usage) | Manual runs go through the engine's authorization and are audited |
| 15.6 | Decision & Model Observatory: Decision Advisor status, routing policies, model usage, costs, routing history, benchmark and evaluation results | Data from `routing_decisions`, `model_usage` and `decision_records` only |
| 15.7 | Engineering Console, read-only: incidents, dead letters, sessions, traces (masked), deployments and infrastructure health, by role | No write action until Phase 19; masked traces |
| 15.8 | Office Communication: conversations, internal messages, task threads, handoff history, search, inline approvals; optional Hermes channel | Messages are business events; no hidden reasoning shown; approvals from chat follow the same server checks; Hermes can notify and deep-link but cannot decide |
| 15.9 | Activity Feed & Audit; Notifications & Approvals; web push with consent (FR-NTF-02) | A push opens the exact item after sign-in; subscriptions are removed on logout |
| 15.10 | Maya and Lia activated; daily and weekly office reviews | Sheets defined, evaluations passed, autonomy approved, routing measured; each review is built from recorded activity and lists its sources |

## Test plan

UI tests per area with recorded fixtures; offline-mode tests; accessibility tests (list views, reduced motion, screen readers) in Hebrew and English; evaluations for Maya (planning and escalation cases) and Lia (reports and approval presentations checked against records); a live demo with real activity.

## Security checklist

- [ ] Views respect roles and shop access; PII masked by default.
- [ ] Chat input is untrusted: Lia and Maya cannot gain tools from conversation content.
- [ ] Approvals from any surface (web, chat, Hermes) pass the same server checks.

## Documentation

`docs/features/office/`, `docs/features/employees/maya.md`, `docs/features/employees/lia.md`; the live-update ADR; the clients' office information architecture.

## Exit gate

- [ ] The office reflects real activity during a live demo; offline mode verified; the owner completes an approval from chat.
- [ ] Maya and Lia are `ready`, their autonomy approved, their routing measured; the first daily and weekly reviews delivered.

## Rollback

Maya's and Lia's kill switches; office areas behind flags; the activity, approvals and tasks screens of Phases 7–10 remain available.

## Risks

| Risk | Mitigation |
|---|---|
| The office shows states that are not real | States only from recorded activity; tests for every state; offline mode |
| Live updates fail behind the platform's request timeout | Spike first; reconnect with cursor; polling fallback |

## Cost impact

Maya's and Lia's model usage, measured; possibly Supabase Realtime usage depending on the spike.
