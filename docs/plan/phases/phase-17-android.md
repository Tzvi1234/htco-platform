# Phase 17 — Android app

| | |
|---|---|
| **Milestone** | — (part of **M6 Clients**, closed in [Phase 18](phase-18-windows.md)) |
| **Repositories** | `htco-clients` (`apps/android`, `design/`, `packages/api-clients`); `htco-platform` (device keys, push registration, notification payloads) |
| **Size** | L |
| **Depends on** | [Phase 7](phase-07-pilot-office.md) to start (runs in parallel with Phases 8–16); each screen also needs the API of the phase that owns it |
| **Requirements** | FR-IAM-06, FR-NTF-02, FR-NTF-03, NFR-SEC-01 (MASVS), NFR-PERF-02, NFR-A11Y-01, NFR-I18N-01; PRD Appendix A scenarios 1, 2, 4, 6, 8 |
| **Mandatory tests owned** | None of the fifteen ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); covers PRD Appendix A cross-platform scenarios 1, 2, 4, 6 and 8 on Android |
| **Status** | PLANNED |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 17.1 Project setup and CI
- [ ] 17.2 Design system in Compose from the shared tokens
- [ ] 17.3 Sign-in, secure storage and session management
- [ ] 17.4 App shell: bottom navigation, shop switcher, contextual floating approvals
- [ ] 17.5 Approvals with biometric step-up
- [ ] 17.6 Push notifications and deep links
- [ ] 17.7 Home, Office and Tasks screens
- [ ] 17.8 Inbox and More screens (screen-matrix parity)
- [ ] 17.9 Offline read and stale data
- [ ] 17.10 Release pipeline, baseline profiles and performance

## Goal

The owner runs the office from the phone. A native Kotlin and Jetpack Compose app shows every screen marked **ALL** in the clients specification's screen matrix (31 of the 43 screens), laid out for a phone: a bottom navigation with **Home, Office, Tasks, Inbox and More**; contextual floating approvals; push notifications that deep-link to the exact order, agent, task or incident; fast Approve and Reject that require authentication for critical actions; charts and business summaries; offline reading of non-sensitive summaries. It looks like HTCO and behaves like Android.

The app holds no business logic. Every state it shows comes from the typed API, and every agent status (IDLE, QUEUED, RUNNING, WAITING_EXTERNAL, WAITING_APPROVAL, BLOCKED, FAILED, OFFLINE) is the one the platform derives from task runs, never a local guess.

## Why now

The pilot of [Phase 7](phase-07-pilot-office.md) is the first time employees ask the owner for decisions. Approvals and incidents reach the owner faster on the phone than on a desk. The core of the app (shell, sign-in, approvals, push, office summary, tasks) can start right after Phase 7; the remaining screens follow the web as each phase's API lands.

## Scope

**In:**

- Compose app with tokens generated from `design/tokens` (the same source as web and Windows); Hebrew RTL and English LTR; light and dark; TalkBack; large touch targets; reduced motion.
- Sign-in decided by an ADR at kickoff: native screens calling Supabase Auth, or the HTCO sign-in page in the system browser through Supabase's OAuth 2.1 server with PKCE, Auth Tab and verified App Links ([A-16](../../product/ASSUMPTIONS.md)). MFA for the owner. Tokens in DataStore encrypted with Tink and a Keystore-wrapped key. Session list and revocation (screen 1).
- Generated Kotlin API client from `packages/api-clients` (OpenAPI), behind a repository layer; realtime events through the authenticated channel, filtered per organization and shop, with dedupe, reconnect and out-of-order protection.
- Bottom navigation and the screen map below; shop switcher (screen 3); contextual floating approvals (a pending approval relevant to the open item is offered in place).
- Approvals: list, approval sheet with requester (person or employee), reason, impact, risk and expiry; Approve and Reject; biometric step-up for critical actions (money, refunds, publishing scope, permissions, high-risk customer messages, production deploy): a Keystore key pair that requires biometrics is registered per device in `device_keys`, the server sends a challenge and verifies the signature before it accepts the decision.
- Push through FCM with deep links to order, agent (employee), task and incident; payloads carry IDs and a generic title only, never customer PII; the app fetches details after authentication.
- Offline read of non-sensitive summaries (dashboard KPIs, office summary, task list headers) with a clear stale label and time; write actions disabled until the app is synchronized.
- Release through Google Play internal, closed and production tracks with staged rollout.

**Out:** the 12 screens marked **WEB/WIN** in the screen matrix (7 Product Discovery, 10 Listing Workspace, 11 Creative Studio, 19 Inter-employee Messenger, 27 Workflow Detail, 29 AI Tools Registry, 30 Decision Advisor, 31 Model Router Console, 37 Engineering & Deployments, 39 Agent Mail Alias Manager, 41 Admin / Integrations, 42 Audit & Compliance). A deep link to one of them opens a short card with a "continue on web" link. No editing of employee profiles, routing or permissions from the phone. No offline writes.

### Screen map (ALL screens on Android)

| Tab | Screens (numbers from the clients specification §3) |
|---|---|
| Sign-in | 1 Welcome & Sign-in (MFA, recovery, authorized sessions) |
| Home | 2 Executive Dashboard; 22 Approvals Center (also as floating approvals everywhere); 36 Incident & Debugging Center (open incidents summary, detail) |
| Office | 14 Virtual Office (department list first, compact map only if it meets the performance budget; the list is always available); 15 Department Detail; 16 Employee Directory; 17 Employee Profile (read only); 18 Employee Chat |
| Tasks | 20 Task Board (list, filters); 21 Task Detail & Agent Runs; 23 Activity Feed |
| Inbox | 40 Notifications; 38 Mail Inbox / Outbox (read, approve drafted sends); 34 Customer Service (tickets, drafted replies, escalations) |
| More | 3 Shop Selector & Settings; 4 Order Queue; 5 Order Detail; 6 Fulfillment Watch; 8 Product Candidate Detail; 9 Catalog Explorer; 12 Supplier Quotes; 13 Pricing & Profitability; 24 Quality Center; 25 Optimization Center; 26 Automation Center; 28 Automation Execution Log; 32 AI Cost & Quota Dashboard; 33 Finance & P&L; 35 Marketing Analytics; 43 Preferences / Offline / Error |

The tab assignment is a starting point for the design review at kickoff; the list of screens is not.

## Owner prerequisites

- Google Play developer account under the business; app signing decision (Play App Signing).
- Firebase project for FCM (no Analytics or Crashlytics data collection unless the owner decides it; Sentry is the crash reporter).
- A test phone with biometrics (mid-range, for the cold-start measurement).
- Approval of the screen map and of the list of actions that need biometric step-up.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification`, `feature-docs` and `design-system` (clients repository) before writing code.
2. Read this file, then [PLAN.md](../PLAN.md), the clients specification §§3, 4, 10, 11 and 13 (kept outside the repository; ask the owner for the current copy if it is not in the session), [PRD](../../product/PRD.md) FR-IAM-06, FR-NTF-02 and Appendix A, [THREAT_MODEL](../../architecture/THREAT_MODEL.md), [TESTING_STRATEGY §5](../../engineering/TESTING_STRATEGY.md#5-clients-summary), and `htco-clients/CLAUDE.md`.
3. Write the sign-in ADR (17.3) before any authenticated screen. Run the A-16 spike first if the system-browser option is chosen.
4. Build in slice order 17.1 → 17.6, then 17.7 and 17.8 screen by screen. A screen is built only when its API exists in the platform and is in `openapi.yaml`; never mock an endpoint in production code to "get ahead". Keep a parity table in `docs/screens/android-parity.md` (screen, API phase, status).
5. For every screen: loading, empty, error, permission-denied, offline and stale states; he/en × light/dark screenshot tests; TalkBack labels. Follow the web's component states from the design system, not new ones.
6. Never put customer PII in a push payload, a log, a crash report or a screenshot test fixture. Never store a token outside encrypted storage. Never compute an agent status on the device.
7. Never add a write action that the API does not check on the server; a hidden button is not a permission.
8. Stop and ask the owner when: a screen's API is missing or behaves differently from the specification; the screen map or the step-up list needs to change; a Play policy or permission (for example notifications, biometrics) needs a decision; a cost appears that is not in [COST_MODEL](../../cost/COST_MODEL.md).

## Slices

### 17.1 — Project setup and CI

Gradle version catalog, Hilt, Retrofit with kotlinx.serialization (or the generated client's stack), ktlint, detekt (advisory), JUnit, Turbine, Roborazzi screenshot tests, `ci-gate` job in the clients repository.

| ID | Acceptance criterion |
|---|---|
| AC-17.1.1 | `ci-gate` is green on a pull request and runs unit, screenshot (he/en × light/dark) and lint jobs. |
| AC-17.1.2 | The generated API client is rebuilt from `openapi.yaml` in CI, and a contract drift fails the build. |

### 17.2 — Design system in Compose

Tokens generated from `design/tokens` into a Compose theme; the shared components the phone needs (AppShell, Avatar, EmployeeCard, DepartmentCard, AgentStatus, TaskCard, MessageThread, ToolResult, ApprovalSheet, AutomationExecutionRow, PolicyChip, CostChart, IncidentCard, ShopSwitcher, OrderTimeline).

| ID | Acceptance criterion |
|---|---|
| AC-17.2.1 | Each component has previews and screenshot tests for its states (default, loading, empty, error, disabled, offline) that match the web's states. |
| AC-17.2.2 | Contrast passes WCAG AA in both themes; IDs, SKUs and tracking numbers stay LTR inside Hebrew text (PRD Appendix A scenario 7 rule). |

### 17.3 — Sign-in, secure storage and session management

| ID | Acceptance criterion |
|---|---|
| AC-17.3.1 | The sign-in ADR is merged before the first authenticated screen. |
| AC-17.3.2 | No token exists outside encrypted storage (instrumented test reads the app's files and preferences). |
| AC-17.3.3 | Logout or server-side revocation clears tokens, cached data, push registration and realtime subscriptions (PRD Appendix A scenario 8). |

### 17.4 — App shell and navigation

Bottom navigation Home, Office, Tasks, Inbox, More; shop switcher; contextual floating approvals; deep-link router.

| ID | Acceptance criterion |
|---|---|
| AC-17.4.1 | A user with access to one shop never sees another shop's data on any screen (PRD Appendix A scenario 2; the server returns 403 or 404 and the app shows the permission state). |
| AC-17.4.2 | A floating approval appears only for an approval that the API lists for the current item and user, and disappears when it is decided elsewhere. |

### 17.5 — Approvals with biometric step-up

Platform part: `device_keys` table, registration endpoint, challenge and signature verification in the approval decision use case. Clients part: approval list and sheet, Approve and Reject, biometric prompt.

| ID | Acceptance criterion |
|---|---|
| AC-17.5.1 | TalkBack reads the approval card with requester, reason, impact and risk level (PRD Appendix A scenario 6). |
| AC-17.5.2 | A critical approval is accepted by the server only with a valid signature from a registered device key over a fresh challenge; a missing, replayed or invalid signature is rejected and audited. |
| AC-17.5.3 | Expired or already decided approvals cannot be decided; the button is disabled and the server refuses anyway. |
| AC-17.5.4 | The owner receives an incident notification and approves or rejects a sensitive fix after re-authenticating (PRD Appendix A scenario 1). |

### 17.6 — Push notifications and deep links

Platform part: push registration per device and user, notification payload builder with a PII check, preferences per severity and shop (FR-NTF-03). Clients part: FCM, notification channels, deep links to order, employee, task and incident.

| ID | Acceptance criterion |
|---|---|
| AC-17.6.1 | A test over every notification type shows that payloads carry only IDs, type and a generic title; no buyer name, address, email or message text. |
| AC-17.6.2 | Each deep link opens the exact item after authentication; a link to a WEB/WIN screen opens the "continue on web" card; a link to an item the user may not see shows the permission state. |
| AC-17.6.3 | Logout removes the push registration on the server. |

### 17.7 — Home, Office and Tasks screens

Screens 2, 22, 36 (Home), 14, 15, 16, 17, 18 (Office), 20, 21, 23 (Tasks), each as soon as its API exists.

| ID | Acceptance criterion |
|---|---|
| AC-17.7.1 | Agent statuses come only from the API; when the worker is offline the office shows OFFLINE and never a running employee (PRD Appendix A scenario 4). |
| AC-17.7.2 | The office list is always available and fully usable with TalkBack; the compact map, if built, respects reduced motion. |
| AC-17.7.3 | Employee Chat sends and shows messages through the HTCO message API with delivery status; the employee profile shows the `billing_mode` badge as the API reports it and offers no switching. |

### 17.8 — Inbox and More screens

Screens 40, 38, 34 (Inbox) and 3, 4, 5, 6, 8, 9, 12, 13, 24, 25, 26, 28, 32, 33, 35, 43 (More), each as soon as its API exists.

| ID | Acceptance criterion |
|---|---|
| AC-17.8.1 | The parity table lists all 31 ALL screens as done, each with its states and screenshot tests. |
| AC-17.8.2 | The Automation Center never shows a run, retry or disable action to a user without that capability (clients specification §7). |
| AC-17.8.3 | The AI Cost & Quota screen shows API cost and credits separately and never shows per-token cost for subscription usage. |

### 17.9 — Offline read and stale data

| ID | Acceptance criterion |
|---|---|
| AC-17.9.1 | Offline, only summaries classified non-sensitive are shown, each labelled with the time of the last sync; order details, customer data and messages are not cached. |
| AC-17.9.2 | Every write action is disabled while offline or unsynchronized, and the reason is shown. |

### 17.10 — Release pipeline and performance

| ID | Acceptance criterion |
|---|---|
| AC-17.10.1 | Signed builds go to Play internal testing from CI; promotion to closed and production tracks uses staged rollout and can be halted. |
| AC-17.10.2 | Cold start is under 1.5 s on the owner's mid-range test phone with baseline profiles (NFR-PERF-02). |

## Test plan

Unit tests for view models and repositories (JUnit, Turbine); screenshot tests he/en × light/dark for every screen and state (Roborazzi); instrumented tests for secure storage, logout and deep links; TalkBack checks on the approval card and the office list; contract tests of the platform endpoints added here (device keys, push registration, notification payloads) with Schemathesis; a payload PII test over every notification type; PRD Appendix A scenarios 1, 2, 4, 6 and 8 on a real device before the exit gate.

**Mandatory tests (PLAN §5).** This phase owns none of the fifteen. It must keep the server-side controls they rely on (approvals, shop scope, audit) as the only authority.

## Security checklist

- [ ] MASVS-aligned review: storage, crypto, auth, network (TLS only, no cleartext), platform interaction, code quality, resilience.
- [ ] Deep links verified (App Links) and validated; no action runs from a link without authentication.
- [ ] No PII in push payloads, logs, crash reports, clipboard or recent-apps thumbnails of sensitive screens (secure flag on those screens).
- [ ] Device keys verified by the server for step-up; no critical decision without a valid signature.
- [ ] No provider, Etsy, Supabase service or runtime secret in the app (the app holds only the user's session).
- [ ] Threat model updated for the mobile client.

## Documentation

`htco-clients/docs/screens/android-parity.md`; `docs/features/android-app/`; the sign-in ADR; `docs/features/approvals/` (step-up); THREAT_MODEL (mobile); DATA_MODEL (`device_keys`, push registrations marked created); README and CLAUDE.md of the clients repository (build, signing, release).

## Demo

From a push notification about an incident, open the incident, read its approval, approve with a fingerprint, and show the decision and the device signature check in the audit trail. Switch to Hebrew and dark mode. Turn on airplane mode and show the stale-labelled summaries with writes disabled. Log out and show that push and cache are cleared.

## Exit gate

- [ ] All 31 ALL screens are on Android with their states (parity table complete).
- [ ] PRD Appendix A scenarios 1, 2, 4, 6 and 8 pass on a real device.
- [ ] The mobile security checklist passed; no open high or critical finding.
- [ ] The owner used the app for a week of real approvals and approved moving on.

## Rollback

Halt the staged rollout in Play; the previous version stays available; server-side features (device keys, push) are behind flags, and the web keeps every capability.

## Risks

| Risk | Mitigation |
|---|---|
| Screens wait for APIs from later phases | Build the core first; track parity per screen; the phase closes only when the APIs exist |
| Push leaks personal data | Payload builder with a PII test over every notification type; details fetched after authentication |
| Biometric step-up bypassed on a rooted device | The server verifies the signature; the private key never leaves the Keystore; critical actions also need a fresh session |
| Native work doubles the effort per feature | No business logic in clients; generated API client; shared tokens |

## Cost impact

Google Play developer account ($25 one-time, [COST_MODEL](../../cost/COST_MODEL.md)); FCM free tier (verify the current terms at kickoff). No model spend.
