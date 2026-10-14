# HTCO OS — MVP Definition

| | |
|---|---|
| **Status** | Draft 1.0 — awaiting owner approval |
| **Last updated** | 2026-10-14 |
| **Related** | [PRD](PRD.md) · [Master plan](../plan/PLAN.md) |

The MVP is the smallest version of HTCO OS that proves the product's thesis: **a business core run by deterministic automations you can trust, plus AI employees that do real work under policy, approvals, a budget and independent supervision.** It is reached in two steps. Each step is usable on its own, so the owner gets value long before the full office exists.

## 1. Two steps to the MVP

| Step | Milestone | Ends with | What the owner can do |
|---|---|---|---|
| **MVP-1: Operational Core** | M2 (end of Phase 7) | Phases 1–7 | Sign in with MFA, connect the Etsy shop, see every order with its verified fulfillment state (all handled by deterministic automations), get one alert per late or missing tracking number, open an incident, approve or reject actions, read the activity and audit trail. No AI yet. |
| **MVP: First Employees** | M3 (end of Phase 11) | Phases 8–11 | Everything above, plus the workforce runtime and tool registry, the model router with per-employee policies and the Decision Advisor benchmark, supervision with the nightly report, and the first employees: **Eitan** (fulfillment exceptions), **Noa** (supervision), and **Daniel** and **Noam** running **Listing Health**: an audit of every active listing against supplier facts, with suggested fixes applied to Etsy only after the owner approves them. |

### Why Listing Health is the first AI workflow

The original planning brief lists Product Studio (new listings) as the first agent department. We recommend starting with Listing Health instead, for four reasons:

1. **It addresses a measured problem.** Public reviews of the pilot shop show "not as described" complaints about size, material and included items. Fixing existing listings protects the shop's rating and reduces refunds right away.
2. **It is low risk.** The agents start read-only (autonomy L0); nothing changes on Etsy without an approval.
3. **It exercises the whole AI stack.** Agent runtime, tool registry, policy engine, approvals, model routing, cost ledger, supervision and evals all get used, so Product Studio (Phase 12) builds on proven parts.
4. **It produces labeled data.** The owner's approve or reject decisions become the gold dataset for evaluating copy and accuracy agents later.

If the owner prefers to start with Product Studio, Phases 11 and 12 swap. Stories US-07 and US-08 and success criterion 4 are then rewritten for Product Studio, and decision D-04 (catalog model and Etsy policy) must be settled before the MVP.

## 2. MVP-1 scope (Operational Core)

### In scope

| Area | Included | Requirements |
|---|---|---|
| Access | Email and password sign-in, password reset and MFA recovery, mandatory TOTP MFA for owner and admin, step-up re-authentication, roles, user and shop-access management | FR-IAM-01..04, 06, FR-ADM-01 |
| Shops | Connect one Etsy shop with OAuth and PKCE, encrypted tokens, capability probe, disconnect | FR-SHOP-01..04 |
| Orders | Automation engine; the Order Intake, Deduplication & Verification and Sales Counters automations; order state machine; list and detail with freshness; units vs orders; reconciliation | FR-ORD-01..06, 08, 09, FR-AUT-01..03 |
| Fulfillment | Tracking Synchronization and Incident Detection & Escalation automations, SLA windows, one incident per gap, automatic resolution on evidence | FR-INC-01..03, 05 |
| Tasks and approvals | Task model, durable queue, approvals with server-side re-check and expiry, policy engine v1, activity records for every actor. Approvals come first from operators' guarded shipment writes and bulk re-sync requests; employees produce them from Phase 9 | FR-TASK-01..03, FR-APR-01..04, FR-AUD-04 |
| Notifications | In-app notifications, email alerts from `noreply@office.ht-co.app` | FR-NTF-01, FR-EML-01 |
| Audit and ops | Append-only audit, structured logs, health checks, error monitoring | FR-AUD-01..03 |
| Clients | Web app only: sign-in, overview, shops, orders, incidents, approvals, audit; Hebrew and English, light and dark | NFR-A11Y-01, NFR-I18N-01 |

### Out of scope for MVP-1

AI employees, product creation, supplier quotes, finance P&L beyond basic order amounts, customer service, inbound email, the office view, Windows and Android apps.

### MVP-1 user stories

| ID | Story | Acceptance (summary) |
|---|---|---|
| US-01 | As the owner, I sign in with my password and authenticator app. | Without MFA, owner-only screens and actions are refused by the API, not just hidden. |
| US-02 | As the owner, I connect my Etsy shop and see which capabilities my app actually has. | The shop card shows granted scopes, last sync, and the result of the shipment-write capability check. |
| US-03 | As the owner, I see every paid order once, with its current fulfillment state and when it was last synced. | Replaying the same Etsy event 10 times leaves exactly one order; data older than the sync window is labeled stale. |
| US-04 | As operations, I am alerted once when an order has no tracking after its SLA window. | One incident and one email per order and cause; the incident closes itself when tracking appears in Etsy. |
| US-05 | As the owner, I approve or reject a pending action and see who decided what and when. | Expired approvals cannot run; deciding twice changes nothing; the audit record shows approver and policy version. |
| US-06 | As a support user, I cannot see shops I was not given. | Cross-shop API calls return 404; the database refuses the read even if the API is bypassed. |

## 3. MVP scope additions (First Employees)

| Area | Included | Requirements |
|---|---|---|
| Workforce runtime | Employee registry and versioned profiles in the database, separate identities, LangGraph runtime behind the port, Postgres checkpoints, interrupts for approvals, resume after restart, internal messages | FR-AGT-01..05, 07, 09, 10, FR-IAM-07 |
| Tools | Tool registry, least-privilege grants, the tool bridge, invocation records | FR-TOOL-01..03, FR-AGT-03, 04 |
| Model router and advisor | Per-employee routing policies, verified model catalog, budget ledger with caps and alerts, routing decision log, PII masking; Decision Advisor with the Jev benchmark | FR-MDL-01..09 |
| Supervision | Deterministic monitors, Noa's exception handling and sampling, the nightly supervisor and daily report | FR-QOS-01..04 |
| First employees | Eitan (fulfillment exceptions), Noa, Daniel and Noam (Listing Health); each with a completed specification sheet and a passed evaluation | FR-AGT-09 |
| Listing Health | Listing audit against supplier facts, accuracy risk score, suggested fixes as approvals, apply approved fixes through the Etsy API, accuracy measured on a labeled sample | FR-QA-01..03, FR-CAT-04 |
| Quality | Eval harness with gold datasets, prompt-injection test fixtures, cost per audited listing | NFR-SEC-01 |
| Clients | Web: employee directory (read-only), activity feed, listing audit review screen, approvals with before and after diff, cost view | — |

### MVP user stories (additions)

| ID | Story | Acceptance (summary) |
|---|---|---|
| US-07 | As the owner, I run a Listing Health audit and see each listing's risk with the evidence behind it. | Every finding cites the supplier fact or listing field it is based on; no finding without evidence. |
| US-08 | As the owner, I approve a suggested fix and see it applied on Etsy. | The Etsy listing changes only after approval; the before and after are stored; a failed write opens an incident. |
| US-09 | As the owner, I set a monthly AI budget and the system respects it. | At 100% of the cap no new employee work starts; safety-critical work uses the reserved budget; alerts at 70% and 90%. |
| US-11 | As the owner, I read a daily operations report I can trust. | Every item links to activity records; unfinished and failed work is listed with an owner; nothing is reported as done without evidence. |
| US-10 | As the owner, I can trust that a malicious instruction hidden in a supplier page changes nothing. | Injection fixtures run in CI; tools stay denied; the finding is flagged as suspicious content. |

## 4. MVP success criteria

The MVP is done when all of these are true in production with the pilot shop:

1. Every MVP-1 and MVP user story passes its acceptance tests in CI and in a recorded demo.
2. Proof-of-concept gates POC-1 to POC-6 pass (defined in [Phase 8](../plan/phases/phase-08-workforce-runtime.md)), and office scenarios A, B, C, E, F, G, H and J pass ([TESTING_STRATEGY §10](../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)).
3. No unapproved external write has happened (verified from the audit log).
4. Listing Health precision on the owner-labeled sample is at least the threshold agreed in Phase 11 (proposed: 85% precision on "high risk" findings).
5. Monthly AI spend stayed within the configured cap, and cost per audited listing is reported.
6. A restore drill and a kill-worker drill have been run and documented.

## 5. Cost envelope (pilot)

The pilot runs on Supabase Pro, a few small DigitalOcean components, an email provider and GitHub Pro, plus AI usage capped by the budget ledger. The figures, scenarios and actuals are kept in one place: [COST_MODEL.md](../cost/COST_MODEL.md).

## 6. After the MVP

Product Studio (Phase 12), suppliers and finance (13), email and customer service (14), the office and the executive team (15), Android (16), Windows (17), continuous optimization (18), the engineering department and self-improvement (19), hardening and v1.0 launch (20), growth and scale (21). See the [master plan](../plan/PLAN.md).
