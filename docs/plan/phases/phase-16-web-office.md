# Phase 16 — The complete web office

| | |
|---|---|
| **Milestone** | — (M6 Clients closes with [Phase 18](phase-18-windows.md)) |
| **Repositories** | `htco-clients` (most work: `apps/web`, `design/`, `packages/api-clients`), `htco-platform` (status projection, live updates, read APIs the screens need, Maya) |
| **Size** | L |
| **Depends on** | [Phase 14](phase-14-governance.md) and [Phase 15](phase-15-mail-support.md). Design work may start during [Phase 7](phase-07-pilot-office.md) |
| **Requirements** | FR-ADM-04, FR-AGT-08, FR-AGT-09, FR-AUD-05, FR-MKT-01 (read views), FR-NTF-02, FR-NTF-03, FR-OFC-01, FR-OFC-02, FR-OFC-03, FR-OFC-04, FR-OFC-05, FR-OFC-06, FR-OFC-07, FR-OFC-08, FR-TASK-04 |
| **Mandatory tests owned** | None new. The screens must show the records that tests 1–15 produce (IDs, links, states) without inventing any |
| **Status** | Planned |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" columns and the exit-gate checklist are the minimum.

## Progress

- [ ] 16.1 Screen matrix, state contract and anti-omission harness
- [ ] 16.2 Live-update spike and ADR
- [ ] 16.3 Real agent statuses and offline mode
- [ ] 16.4 UX1 audit: authentication, home, shops, orders
- [ ] 16.5 UX2: Office 2D, departments and directory
- [ ] 16.6 UX2: Employee profile
- [ ] 16.7 UX2: Conversations, tasks, approvals and activity
- [ ] 16.8 UX3: Automations, tools, supervision, optimization, models and quota
- [ ] 16.9 UX4: Catalog, images, pricing, finance, support and marketing
- [ ] 16.10 UX5: Mail, engineering, notifications, administration and audit
- [ ] 16.11 Maya at full scope; daily and weekly office reviews
- [ ] 16.12 Anti-omission gate

## Goal

The owner works in the whole office on the web: **all 43 screens** of the clients specification's screen matrix, each built on authoritative records (CRM, agent runs, automation executions, provider data) and each with loading, empty, error, permission and offline states. The office shows the organization exactly as it is: seven people departments with sixteen employees, three service wings (Automations, AI Tools, Infrastructure) that are visibly **not people**, Jev as a shared service tile, and image generation inside Nika's studio. Agent statuses are real runtime states derived from task runs, never cartoons. **Maya** (General Manager) moves from pilot scope to full scope: she plans and distributes work across all departments and runs the daily and weekly office reviews.

## Why now

Every department now has real work and real records: commerce (Phases 9–13), governance (14), mail and the owner assistant (15). Building the full office earlier would have meant empty or invented states. Earlier phases built many screens for their own features; this phase completes the rest, brings all of them to one standard, and proves nothing was left out.

## Scope

**In:**

- The 43 screens on the web, organized by the clients specification's stages UX2–UX5, plus an audit of the UX1 screens built in earlier phases (table below).
- One screen state contract (loading, empty, error, permission, offline, stale, disabled) used by every screen; a screen matrix document and an automated anti-omission harness.
- Live updates over authenticated, organization- and shop-filtered channels with replay, reconnect, deduplication and out-of-order handling (spike first, A-12).
- Real agent statuses `IDLE`, `QUEUED`, `RUNNING`, `WAITING_EXTERNAL`, `WAITING_APPROVAL`, `BLOCKED`, `FAILED`, `OFFLINE` derived from task runs, agent runs and heartbeats.
- Office 2D with an accessible list alternative, reduced motion and a performance budget.
- Employee profile with thirteen tabs and the `billing_mode` badge.
- Conversations of four kinds (user → employee, employee → employee, employee → department, employee → task) over the HTCO message bus.
- Web push with consent and deep links.
- Maya's full-scope activation; daily and weekly office reviews.

**Out:**

- Windows and Android parity ([Phase 18](phase-18-windows.md), [Phase 17](phase-17-android.md)); the web is the reference they copy.
- Write actions in the Engineering screens beyond what the policy allows today ([Phase 19](phase-19-engineering.md)).
- UX6 release work (performance at scale, release runbooks), finished in [Phase 20](phase-20-hardening-release.md); accessibility and RTL/LTR are checked per screen here.
- Marketing actions and campaigns (Gal, [Phase 21](phase-21-growth-scale.md)); Marketing Analytics is read-only here.

### Screen matrix by stage

Numbers are the clients specification's screen numbers. "Built in" names the phase that first delivered the screen, if any; this phase completes it to the state contract or builds it.

| Stage | # | Screen | Built in |
|---|---|---|---|
| UX1 (audit) | 1 | Welcome & Sign-in (MFA, recovery, sessions) | [3](phase-03-design-system-web-shell.md) |
| UX1 (audit) | 2 | Executive Dashboard (shop KPIs, net profit, urgent approvals, daily brief) | [7](phase-07-pilot-office.md) basic; completed here |
| UX1 (audit) | 3 | Shop Selector & Settings | [3](phase-03-design-system-web-shell.md), [9](phase-09-etsy-connection.md) |
| UX1 (audit) | 4 | Order Queue | [4](phase-04-durable-core-order-intake.md), [9](phase-09-etsy-connection.md) |
| UX1 (audit) | 5 | Order Detail (timeline, Etsy/AutoVA sync proof) | [4](phase-04-durable-core-order-intake.md), [10](phase-10-fulfillment-incidents.md) |
| UX1 (audit) | 6 | Fulfillment Watch | [10](phase-10-fulfillment-incidents.md) |
| UX1 (audit) | 43 | Preferences / Offline / Error | [3](phase-03-design-system-web-shell.md) |
| UX2 | 14 | Virtual Office 2D | here |
| UX2 | 15 | Department Detail | here |
| UX2 | 16 | Employee Directory | [6](phase-06-workforce-claude-runtime.md) (workforce directory) |
| UX2 | 17 | Employee Profile | here |
| UX2 | 18 | Employee Chat | [15](phase-15-mail-support.md) (Lia only) |
| UX2 | 19 | Inter-employee Messenger | here |
| UX2 | 20 | Task Board | [5](phase-05-tasks-messages-approvals.md) |
| UX2 | 21 | Task Detail & Agent Runs | [5](phase-05-tasks-messages-approvals.md), [6](phase-06-workforce-claude-runtime.md) |
| UX2 | 22 | Approvals Center | [5](phase-05-tasks-messages-approvals.md) |
| UX2 | 23 | Activity Feed | [5](phase-05-tasks-messages-approvals.md) |
| UX3 | 24 | Quality Center | [14](phase-14-governance.md) |
| UX3 | 25 | Optimization Center | [14](phase-14-governance.md) |
| UX3 | 26 | Automation Center | here |
| UX3 | 27 | Workflow Detail | here |
| UX3 | 28 | Automation Execution Log | here |
| UX3 | 29 | AI Tools Registry | [8](phase-08-models-tools.md) |
| UX3 | 30 | Decision Advisor / Jev | [8](phase-08-models-tools.md) |
| UX3 | 31 | Model Router Console | [8](phase-08-models-tools.md) |
| UX3 | 32 | AI Cost & Quota Dashboard | [8](phase-08-models-tools.md) |
| UX4 | 7 | Product Discovery | [7](phase-07-pilot-office.md) (research results), [12](phase-12-product-studio.md) |
| UX4 | 8 | Product Candidate Detail | [12](phase-12-product-studio.md) |
| UX4 | 9 | Catalog Explorer | [12](phase-12-product-studio.md) |
| UX4 | 10 | Listing Workspace | [11](phase-11-listing-health.md), [12](phase-12-product-studio.md) |
| UX4 | 11 | Creative Studio (Nika) | [12](phase-12-product-studio.md) |
| UX4 | 12 | Supplier Quotes | [13](phase-13-procurement-finance.md) |
| UX4 | 13 | Pricing & Profitability | [13](phase-13-procurement-finance.md) |
| UX4 | 33 | Finance & P&L | [13](phase-13-procurement-finance.md) |
| UX4 | 34 | Customer Service | [15](phase-15-mail-support.md) |
| UX4 | 35 | Marketing Analytics (read-only) | here |
| UX5 | 36 | Incident & Debugging Center | [10](phase-10-fulfillment-incidents.md) |
| UX5 | 37 | Engineering & Deployments (read-only until 19) | here |
| UX5 | 38 | Mail Inbox / Outbox | [15](phase-15-mail-support.md) |
| UX5 | 39 | Agent Mail Alias Manager | [15](phase-15-mail-support.md) |
| UX5 | 40 | Notifications (with web push) | here |
| UX5 | 41 | Admin / Integrations | [3](phase-03-design-system-web-shell.md) |
| UX5 | 42 | Audit & Compliance | here |

At kickoff, check this table against what each phase really delivered and correct the "Built in" column in the screen matrix document.

## Owner prerequisites

- Design review of the Office 2D layout, department rooms and service wings (design system v2, DS0 from [Phase 3](phase-03-design-system-web-shell.md)); the original HTCO logo is used, never recreated.
- Approval of Maya's full-scope autonomy (proposed L1) and of her evaluation set.
- Format, time and recipients of the daily and weekly office reviews.
- Consent wording for web push.
- Confirmation of the screen matrix document (what each screen must show) before slice 16.4.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification`, `feature-docs` and, for all `htco-clients` work, `design-system`.
2. Read, in order: this file; [PLAN.md](../PLAN.md) §5–§6; the clients specification v6.0 (kept outside the repository: §2 navigation, §3 screen matrix, §5 office, §6 employee page, §7 automations and supervisor and optimizer UI, §8 models and tools screens, §9 mail, §11 API and security, §12 roadmap, §13 the anti-omission checklist); [ADR-0012](../../adr/0012-living-office-organization.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ADR-0018](../../adr/0018-image-generation-specialist.md); `PERMISSION_MATRIX.md`; the clients repository's design system docs and `docs/screens/`.
3. Work the slices in order: 16.1 (matrix and harness first, so every later slice is checked), 16.2 → 16.3 (live state), 16.4 (UX1 audit), then UX2 → UX3 → UX4 → UX5 (16.5–16.10), 16.11 (Maya; may start once 16.5–16.7 are merged), 16.12 last.
4. For each slice: acceptance criteria → failing tests (component, contract and end-to-end with recorded fixtures) → code → verify in the deployed web app against real records → docs → one pull request per screen group.
5. The UI calls only typed API contracts from `packages/api-clients`. If a screen needs data that no API returns, add the read API in `htco-platform` first (contract-first in `openapi.yaml`), in its own pull request.
6. Never in this phase: show a status, number or employee activity that does not come from a record; animate an employee as working when the worker is offline or the employee is `draft`; show a "Run", "Approve" or "Retry" control to a role that lacks the capability (hiding is not enough: the backend refuses too); show model reasoning; invent per-token cost for subscription usage or offer a control that switches billing mode; show Jev or the image engine as an employee; let a profile edit take effect without a policy approval and a new profile version; put a secret, token or `service_role` key in the client.
7. Stop and ask the owner when: a screen in the matrix has no source of truth yet; the spike shows that live updates cannot meet the requirements on the current platform; a design decision changes the department structure or the service wings; a checklist item cannot be met.

## Employees activated

| Employee | Registry | Work |
|---|---|---|
| Maya, General Manager (full scope; pilot scope since Phase 7) | [`maya.toml`](../../../registry/employees/maya.toml) | Receives objectives from the owner through Lia; plans and distributes tasks across all departments; coordinates departments; escalates with evidence and options; prioritizes Omer's proposals; runs the daily and weekly office reviews; business owner of Agent Task Scheduling & Recovery |

Maya follows the employee activation rule of [Phase 7](phase-07-pilot-office.md); full scope is a new profile version with a new evaluation.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 16.1 | Screen matrix document (`htco-clients/docs/screens/SCREEN_MATRIX.md`: 43 rows with route, data sources, actions, capabilities, states) and an automated harness that fails CI when a matrix screen lacks a route, a story or a test for any of its states | All 43 screens listed; CI fails on a missing state |
| 16.2 | Live-update spike and ADR (server-sent events with reconnect through DigitalOcean's request timeout, or Supabase Realtime private channels; A-12) | Reconnect and replay from a cursor; duplicates and out-of-order updates ignored; channels filtered per organization and shop; no state derived only from the stream |
| 16.3 | Status projection in `htco-platform` from task runs, `agent_runs` and heartbeats; offline mode | Each of the eight statuses has a test that seeds its records; worker offline → office read-only, nobody shown as running; a `draft` employee is never shown as working |
| 16.4 | UX1 audit: screens 1–6 and 43 brought to the state contract; the Executive Dashboard completed (KPIs, net profit, urgent approvals, daily brief from Maya's review) | Every figure links to its records |
| 16.5 | Office 2D (14), Department Detail (15), Employee Directory (16) | See below |
| 16.6 | Employee Profile (17) | See below |
| 16.7 | Employee Chat (18), Inter-employee Messenger (19), Task Board (20), Task Detail & Agent Runs (21), Approvals Center (22), Activity Feed (23) | See below |
| 16.8 | UX3: Automation Center (26), Workflow Detail (27), Execution Log (28); Quality Center (24) and Optimization Center (25) fitted into the shell; AI Tools Registry (29), Decision Advisor / Jev (30), Model Router Console (31), AI Cost & Quota (32) completed | Real executions and audit links; no "Run" without capability; quota panel separates API cost, credits and subscription information |
| 16.9 | UX4: screens 7–13 and 33–35 completed to the state contract; Marketing Analytics (35) built read-only on Product Metrics Rollup | End-to-end domain policies visible (margin floor, policy checks, approvals); no double-counted sale |
| 16.10 | UX5: Incident & Debugging (36), Engineering & Deployments (37, read-only), Mail (38, 39) fitted into the shell, Notifications (40) with web push, Admin / Integrations (41), Audit & Compliance (42) | Push opens the exact item after sign-in; subscriptions removed on logout; audit is append-only and exportable with permission |
| 16.11 | Maya at full scope; daily and weekly office reviews | See below |
| 16.12 | Anti-omission gate | See below |

### 16.5 — Office 2D, departments and directory

The office is a building with rooms for the seven people departments (Executive, Product Studio, Procurement & Finance, Order Assurance, Marketing, Support, Engineering) and three service wings (Automations, AI Tools, Infrastructure). Each employee tile opens the employee's profile; department rooms show current work, handoffs and KPIs. Service wings show dashboards, services and workflows, never avatars. Jev is a shared service tile linked to the Decision Advisor screen. Nika's tile links to her Creative Studio, where image generation lives. Motion is smooth and subtle, off under reduced motion, within a performance budget. On mobile widths and for screen readers the office is a list that carries the same information.

| ID | Acceptance criterion |
|---|---|
| AC-16.5.1 | All 16 employees appear in their 7 departments with the right hierarchy (Owner → Maya → departments; Noa and Omer in the Executive Office). |
| AC-16.5.2 | The three service wings are visibly distinct from people departments and contain no employee avatars; Jev appears only as a shared service tile; image generation appears only in Nika's studio. |
| AC-16.5.3 | The list alternative carries every status, assignment and link of the 2D view (automated comparison); keyboard and screen-reader tests pass in Hebrew (RTL) and English (LTR). |
| AC-16.5.4 | Every status shown is one of the eight real statuses from 16.3, with a link to the task run it came from. |

### 16.6 — Employee profile

Thirteen tabs: Overview, Responsibilities, Tasks, Conversations, Agent Sessions, Skills & Tools & Automations, Knowledge, Memory, Model Router, Permissions, Evaluations, Cost & Usage, History. The profile shows the profile version in use, its evaluation result and its runtime. A `billing_mode` badge shows how runs were paid (API, credit, or a subscription mode only where it is legally and technically supported, per [ADR-0017](../../adr/0017-auth-and-billing-policy.md)), with the data source. Edits never apply in place: a change creates a proposed profile version that goes through the reviewed registry path and the policy approval, and only then becomes active.

| ID | Acceptance criterion |
|---|---|
| AC-16.6.1 | Each tab reads from records (profile versions, tasks, messages, `agent_runs`, `tool_invocations`, `evaluation_runs`, `model_usage`, memory items) and has all states of the contract. |
| AC-16.6.2 | The `billing_mode` badge matches `agent_runs.billing_mode` for the period shown; there is no control that switches billing mode, and subscription usage never shows invented per-token cost. |
| AC-16.6.3 | Saving a profile edit creates a proposed version and an approval; the running version stays unchanged until the approval and the sync complete (test). |
| AC-16.6.4 | Memory and sessions show business summaries and receipts, never model reasoning (FR-AGT-08). |

### 16.7 — Conversations, tasks, approvals and activity

Conversations are a view over the HTCO message bus (`employee_messages`), not a direct connection to a Claude session. Four kinds: user → employee, employee → employee, employee → department, employee → task. Messages carry structured attachments, delivery status and acknowledgement receipts. Every handoff shows its `message_id` and the task transition it caused. The Task Board gets board, list with SLA, and timeline views (FR-TASK-04); Task Detail shows agent runs with trace, stages, cost, evidence and resume history.

| ID | Acceptance criterion |
|---|---|
| AC-16.7.1 | A handoff shown in the messenger links to exactly one message and one task transition; delivery and ACK states come from the message record. |
| AC-16.7.2 | A user sees only conversations and tasks of shops they may access; the backend refuses the rest (test with a role lacking the shop). |
| AC-16.7.3 | Approving from a conversation, the Approvals Center or a push uses the same server re-authorization and records the same approval. |
| AC-16.7.4 | Activity is filterable by actor, department, shop, task, kind and time, and answers "what did Shon do today" from records (FR-AUD-05). |

### 16.11 — Maya at full scope; daily and weekly office reviews

Maya plans and distributes tasks across all departments, coordinates handoffs, escalates with evidence and options, and prioritizes Omer's proposals. She runs the daily and weekly office reviews from recorded activity, delivered by Scheduled Reporting and presented by Lia. She cannot bypass approval gates, edit the ledger, change grants or approve her own requests.

| ID | Acceptance criterion |
|---|---|
| AC-16.11.1 | Maya's full-scope profile version has no `draft` section; her evaluation set (planning, distribution and escalation cases across departments) passes; the owner approved her autonomy; her routing is measured. |
| AC-16.11.2 | Each office review is built from recorded activity and lists its sources; a missing source is named, never filled in. |
| AC-16.11.3 | Maya's attempts to approve, change a grant, edit the ledger or bypass a gate are denied by the tool bridge and recorded. |

### 16.12 — Anti-omission gate

The clients specification's "UI acceptance and anti-omission checklist", each item an automated test or a recorded manual check in the screen matrix document:

| ID | Checklist item |
|---|---|
| AC-16.12.1 | All 16 employees and 7 people departments appear, with the correct hierarchy. |
| AC-16.12.2 | The three service departments are visibly distinct from people. |
| AC-16.12.3 | GPT image capability appears in Nika's studio; Jev appears in Decision Advisor and router screens; neither is shown with policy authority. |
| AC-16.12.4 | Noa's Quality Center shows audits of CRM activity, not text summaries of chats. |
| AC-16.12.5 | Omer's proposals link to their GitHub implementation and post-release measurements. |
| AC-16.12.6 | Every agent action, automation execution, critical email, approval and audit event shown has its ID, authoritative record and link. |
| AC-16.12.7 | The router screens show each employee's policy with provenance, budget and fallback. |
| AC-16.12.8 | Subscription usage is shown only where legally and technically supported; API cost and credits are separate. |
| AC-16.12.9 | Push links support Approve and Reject with authentication and audit (web here; mobile in Phase 17). |
| AC-16.12.10 | Layout adapts across widths without duplicating domain logic in the client (shared contracts; parity with Windows and Android checked in Phases 17–18). |
| AC-16.12.11 | Office 2D has an accessible list alternative. |
| AC-16.12.12 | All 43 screens have empty, error, permission, loading and offline states (harness of 16.1 green). |
| AC-16.12.13 | Role access by shop is enforced in the backend, not only by hidden buttons (API tests for every action the screens offer). |
| AC-16.12.14 | Inbound and outbound email and the agent alias domain have their screens and DNS in place. |
| AC-16.12.15 | No duplicated Etsy shipment update or double-counted sale appears in any screen (cross-check with the reconciliation records). |

## Test plan

Component tests with stories per state; contract tests against the generated API client; end-to-end tests with recorded fixtures per screen group; status projection tests per status; live-update tests (reconnect, replay, duplicates, out of order, shop filtering); offline-mode tests; accessibility tests (keyboard, screen reader, reduced motion, contrast) in Hebrew and English, light and dark; backend permission tests for every action a screen offers; Maya's evaluation set; a live demo on real activity.

## Security checklist

- [ ] Views respect roles and shop access, enforced in the backend; PII masked by default.
- [ ] Secure HttpOnly cookies (BFF from Phase 3); no secret, provider key, Etsy token or `service_role` in the client.
- [ ] Realtime channels filtered per organization and shop; replay and reconnect protected.
- [ ] Chat input is untrusted: Maya, Lia and every employee gain no tools from conversation content.
- [ ] Approvals from any surface (web, chat, push, Hermes) pass the same server checks.
- [ ] Anti-XSS and CSRF tests; attachments shown only after scanning.
- [ ] The [threat model](../../architecture/THREAT_MODEL.md) updated for live updates and push.

## Documentation

`htco-clients/docs/screens/SCREEN_MATRIX.md`; `docs/ux/` office information architecture; `docs/features/office/` and `docs/features/employees/maya.md` in the platform; the live-update ADR; PRD FR-OFC-01 updated to the eight real statuses; [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios) links to the screen tests.

## Demo

Walk the owner through the office on real activity: the 2D office and its list view, a department room, an employee profile with all tabs and the `billing_mode` badge, a handoff in the messenger with its message and task transition, an approval from a conversation and from a web push, the Quality and Optimization Centers, the quota panel, mail and aliases. Stop the agent worker and show the office go read-only with nobody running. Show the anti-omission checklist report.

## Exit gate

- [ ] All 43 screens pass the harness; all AC-16.x pass, including the fifteen anti-omission items.
- [ ] The office reflects real activity during a live demo; offline mode verified.
- [ ] Maya is `ready` at full scope, her autonomy approved, her routing measured; the first daily and weekly reviews delivered.

## Rollback

Maya's kill switch (her pilot-scope version can be restored); office areas behind flags; the screens of earlier phases stay available on their routes.

## Risks

| Risk | Mitigation |
|---|---|
| The office shows states that are not real | States only from records; a test per status; offline mode; the anti-omission harness |
| Live updates fail behind the platform's request timeout | Spike first; reconnect with a cursor; polling fallback |
| 43 screens drift from one standard | One state contract and one harness for all of them, checked in CI |
| Scope creep in the 2D office | Design review first; the list view is the source of truth; animation is optional polish |

## Cost impact

Maya's model usage, measured with `billing_mode = API_PRODUCTION`. Possibly Supabase Realtime usage, depending on the spike (verify pricing at kickoff). Static web hosting unchanged.
