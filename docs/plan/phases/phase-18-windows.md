# Phase 18 — Windows app

| | |
|---|---|
| **Milestone** | **M6 Clients** |
| **Repositories** | `htco-clients` (`apps/windows`, `design/`, `packages/api-clients`); `htco-platform` (Windows device keys and notifications, if not already done in Phase 17) |
| **Size** | L |
| **Depends on** | [Phase 16](phase-16-web-office.md) (runs in parallel with Phase 19); [Phase 17](phase-17-android.md) for the shared sign-in ADR and device keys |
| **Requirements** | FR-ADM-04, FR-IAM-06, FR-NTF-02, NFR-SEC-01, NFR-PERF-02, NFR-A11Y-01, NFR-I18N-01; PRD Appendix A scenarios 2, 3, 4, 5, 8 |
| **Mandatory tests owned** | None of the fifteen ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); covers PRD Appendix A cross-platform scenarios 2, 3, 4, 5 and 8 on Windows; the Developer Console must not open a path around test 9 |
| **Status** | PLANNED |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 18.1 RTL and navigation spike
- [ ] 18.2 Solution setup and CI
- [ ] 18.3 Sign-in, secure storage, Windows Hello step-up
- [ ] 18.4 Three-column workspace, command palette and global search
- [ ] 18.5 Split-pane workspaces: Orders, Listings, Profitability, Incidents, Agent conversations
- [ ] 18.6 Office, work, commerce, product and finance screens (screen-matrix parity)
- [ ] 18.7 Developer Console (read only, policy-gated)
- [ ] 18.8 Multi-window and Windows notifications
- [ ] 18.9 Signed MSIX, SemVer, staged rollout and rollback

## Goal

A native Windows workspace for long working sessions, built with C#/.NET and WinUI 3, that covers **all 43 screens** of the clients specification's screen matrix (the ALL screens and the WEB/WIN screens). It is an analytic tool, not a phone app on a big screen: a **three-column workspace** (navigation, list, detail and inspector), compact dense tables, keyboard shortcuts and a global search, **split panes** for Orders, Listings, Profitability, Incidents and Agent conversations, and multi-window where it helps. The **Developer Console** shows traces, CI runs, GitHub issues, pull requests and staged diffs, but it cannot execute a production command; any action goes through the backend policy gate. Releases are signed MSIX packages with SemVer, a staged rollout, an update source and a rollback path.

As on every client, the app holds no business logic, and every status it shows comes from the API.

## Why now

The complete web office of [Phase 16](phase-16-web-office.md) defines every screen and every API. Windows then adds what the web does less well: dense keyboard-driven tables, several windows side by side, operating-system notifications and a local read cache. With Android ([Phase 17](phase-17-android.md)) it completes milestone M6.

## Before starting

At kickoff, confirm with the owner the concrete reasons for the native Windows app (multi-window work, keyboard-heavy grids, OS notifications, offline read cache), so the effort targets what the web cannot do well. Three native clients cost roughly two to two and a half times the effort of web-only per feature. Record the answer in the phase's feature doc.

## Scope

**In:**

- WinUI 3 on the Windows App SDK (version verified at kickoff); MVVM with CommunityToolkit.Mvvm and dependency injection; view models in a plain .NET library with no UI dependency.
- Tokens from `design/tokens` as XAML theme dictionaries (light, dark, high contrast); density setting (compact by default for tables); Hebrew RTL and English LTR; Narrator; full keyboard operation; reduced motion; 60 fps in office animations (NFR-PERF-02).
- Generated client (Kiota or the generator chosen for `packages/api-clients`) behind an `IHtcoApi` facade; realtime events filtered per organization and shop, with dedupe, reconnect and out-of-order protection.
- Sign-in per the ADR of [Phase 17](phase-17-android.md) (system browser with a loopback redirect and PKCE, or native screens); tokens in Credential Locker (packaged) or DPAPI; Windows Hello step-up with a device key in `device_keys` whose signature the server verifies.
- **Three-column workspace:** navigation rail (the information architecture of the clients specification §2), list or table, detail with an inspector; a command palette (Ctrl+K) and a global search across orders, listings, tasks, employees, incidents and messages, limited to the user's shops.
- **Split panes** for Orders, Listings, Profitability, Incidents and Agent conversations (list and detail, or two items compared).
- **Multi-window:** open an order, an incident, a task or a conversation in its own window; windows share one session and one realtime connection.
- All 43 screens (see the parity table below).
- **Developer Console** (screens 36 and 37 and the engineering parts of 21): traces, CI runs, GitHub issues, pull requests and staged diffs, deployments and infrastructure health. Read only in this phase. The safe actions of [Phase 19](phase-19-engineering.md) (approve a release gate, retry a dead letter, run a dry-run) are added when they exist, always as API calls through the policy engine, with step-up and audit. There is no shell, no free-form command box and no direct call to GitHub, DigitalOcean or Supabase from the app.
- Windows notifications with deep links to order, employee, task and incident; no customer PII in notification text.
- Local read cache of non-sensitive summaries, labelled stale; writes disabled until synchronized.
- MSIX signed with Azure Artifact Signing (or the signing service chosen at kickoff); SemVer; App Installer update source (and WinGet if the owner wants it); staged rollout by channel (internal, then owner, then all users); rollback to the previous version.

**Out:** an Electron or web-wrapper shortcut (needs a new ADR, clients specification §1); editing employee profiles, routing or permissions outside the approval and pull-request paths; any production command from the Developer Console.

### Screen parity

| Group | Screens (numbers from the clients specification §3) | Windows layout |
|---|---|---|
| ALL (31) | 1–6, 8, 9, 12–18, 20–26, 28, 32–36, 38, 40, 43 | Same content as web; dense tables and split panes where listed above |
| WEB/WIN (12) | 7 Product Discovery, 10 Listing Workspace, 11 Creative Studio, 19 Inter-employee Messenger, 27 Workflow Detail, 29 AI Tools Registry, 30 Decision Advisor, 31 Model Router Console, 37 Engineering & Deployments, 39 Agent Mail Alias Manager, 41 Admin / Integrations, 42 Audit & Compliance | Full workspace screens; 37 is part of the Developer Console |

## Owner prerequisites

- Code-signing account under the business (Azure Artifact Signing or an alternative). Identity validation can take weeks; start it early (verify the current lead time).
- Decision on distribution: App Installer from an HTCO-controlled HTTPS location, WinGet, or both.
- Answer to the "why native Windows" question above.
- A clean Windows test machine (or VM) for install and update tests.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification`, `feature-docs` and `design-system` (clients repository).
2. Read this file, then [PLAN.md](../PLAN.md), the clients specification §§2–4, 10, 11 and 13 (ask the owner for the current copy if it is not in the session), [PRD](../../product/PRD.md) FR-ADM-04 and Appendix A, [TESTING_STRATEGY §5](../../engineering/TESTING_STRATEGY.md#5-clients-summary), the sign-in ADR from Phase 17 and `htco-clients/CLAUDE.md`.
3. Do 18.1 (RTL spike) first and record the decision before building any screen. Then 18.2 → 18.5, then screens in 18.6 in the order of the web parity table, then 18.7 → 18.9.
4. Reuse the web's screen definitions and the shared component states; do not design new states. Keep `docs/screens/windows-parity.md` (screen, layout, status).
5. Every screen has loading, empty, error, permission-denied, offline and stale states; screenshot comparisons (he/en × light/dark × high contrast); Narrator names; a keyboard path.
6. Never give the Developer Console a way to run a command, call an external system directly or hold a credential other than the user's session. Every action is an API call that the server authorizes.
7. Never store a token in plain files or app settings. Never compute an agent status on the client.
8. Stop and ask the owner when: a WinUI or Windows App SDK limitation blocks RTL or a required interaction; signing or distribution needs a decision or money; an API needed by a screen is missing; the "why native" answer narrows the scope.

## Slices

### 18.1 — RTL and navigation spike

NavigationView, title bar, flyouts, data grid and split panes in Hebrew, with mixed LTR content (IDs, SKUs, tracking numbers, code).

| ID | Acceptance criterion |
|---|---|
| AC-18.1.1 | A decision record lists the controls that work in RTL, the workarounds, and the data grid chosen; it is merged before screens are built. |

### 18.2 — Solution setup and CI

| ID | Acceptance criterion |
|---|---|
| AC-18.2.1 | `ci-gate` is green on `windows-latest`: build, MSTest for view models, WinUI UI tests, FlaUI smoke test, analyzers with nullable enabled. |
| AC-18.2.2 | The API client is regenerated from `openapi.yaml` in CI, and drift fails the build. |

### 18.3 — Sign-in, secure storage, Windows Hello step-up

| ID | Acceptance criterion |
|---|---|
| AC-18.3.1 | No token in plain files, settings or logs (test inspects the package's local data). |
| AC-18.3.2 | A critical approval needs a Windows Hello signature that the server verifies; a missing or invalid signature is rejected and audited. |
| AC-18.3.3 | Logout or revocation clears tokens, cache, notification registration and realtime subscriptions in every open window (PRD Appendix A scenario 8). |

### 18.4 — Three-column workspace, command palette and global search

| ID | Acceptance criterion |
|---|---|
| AC-18.4.1 | Every primary action has a keyboard shortcut listed in an in-app shortcut sheet; focus order is logical in RTL and LTR. |
| AC-18.4.2 | Global search returns only items from the user's shops (server-side), and opens results in the current pane or a new window. |
| AC-18.4.3 | A Support user cannot reach an order from a shop they may not see, through search, deep link or a new window (PRD Appendix A scenario 2). |

### 18.5 — Split-pane workspaces

Orders, Listings, Profitability, Incidents and Agent conversations.

| ID | Acceptance criterion |
|---|---|
| AC-18.5.1 | A keyboard-only user can find an order and open its incident detail (PRD Appendix A scenario 5). |
| AC-18.5.2 | Dense tables virtualize; scrolling a table of 10,000 orders stays smooth on the test machine (target set at kickoff). |
| AC-18.5.3 | A tracking number updated in Etsy appears with its source and timestamp, without duplication (PRD Appendix A scenario 3). |

### 18.6 — Screen-matrix parity

| ID | Acceptance criterion |
|---|---|
| AC-18.6.1 | The parity table lists all 43 screens as done, each with its states, screenshot comparisons and Narrator names. |
| AC-18.6.2 | The office opens read-only when the worker is offline and never shows a running employee (PRD Appendix A scenario 4); office animations hold 60 fps and respect reduced motion. |
| AC-18.6.3 | Actions are shown only when the user has the capability, and the server refuses them anyway (role matrix test through the API). |
| AC-18.6.4 | Routing, Decision Advisor and AI cost screens show provenance, budget and fallback per employee, and show API cost and credits separately from subscription usage, as on the web. |

### 18.7 — Developer Console (read only, policy-gated)

| ID | Acceptance criterion |
|---|---|
| AC-18.7.1 | The console shows traces (masked), CI runs, GitHub issues, pull requests, staged diffs, deployments and infrastructure health from the API only. |
| AC-18.7.2 | There is no shell, command box or direct external call; a static check over the Windows code fails the build if it references a GitHub, DigitalOcean or Supabase admin endpoint or SDK. |
| AC-18.7.3 | Any action button (added with Phase 19) calls an API route that the policy engine checks, needs step-up when high-risk, and writes an audit record; without the capability the button is not shown and the route returns 403. |

### 18.8 — Multi-window and Windows notifications

| ID | Acceptance criterion |
|---|---|
| AC-18.8.1 | Several windows share one session and one realtime connection; closing the main window does not leave a window with a stale session. |
| AC-18.8.2 | Notifications deep-link to the exact item after authentication and contain no customer PII. |

### 18.9 — Signed MSIX, SemVer, staged rollout and rollback

| ID | Acceptance criterion |
|---|---|
| AC-18.9.1 | The signed MSIX installs and updates on a clean machine from the update source; the version follows SemVer and matches the release notes. |
| AC-18.9.2 | A release goes to the internal channel, then the owner, then all users; it can be stopped at each step. |
| AC-18.9.3 | Rolling back to the previous version is a documented, tested procedure; local cache from the newer version does not break the older one. |

## Test plan

MSTest for view models; WinUI UI tests for controls; FlaUI smoke tests for the main journeys; screenshot comparisons (he/en × light/dark × high contrast); Narrator and keyboard checks; install, update and rollback tests on a clean machine; PRD Appendix A scenarios 2, 3, 4, 5 and 8.

**Mandatory tests (PLAN §5).** This phase owns none of the fifteen. The Developer Console must not create a path around mandatory test 9 (a GitHub agent pushing to production) or around the owner's release gate.

## Security checklist

- [ ] Tokens only in Credential Locker or DPAPI; no secrets in the package, settings or logs.
- [ ] Windows Hello device keys verified by the server for step-up.
- [ ] Developer Console: masked traces, no command execution, no direct external calls; every action through the API and audited.
- [ ] Signed MSIX; update source over HTTPS under HTCO's control; signature checked on install.
- [ ] Notifications without customer PII.
- [ ] Threat model updated for the Windows client and the Developer Console.

## Documentation

`htco-clients/docs/screens/windows-parity.md`; `docs/features/windows-app/`; the RTL decision record; release and rollback runbook for MSIX; THREAT_MODEL; README and CLAUDE.md of the clients repository; PLAN status for M6.

## Demo

Keyboard only: open the command palette, search an order, open it in a split pane next to its incident, open the incident in a second window. Show the Developer Console with a pull request, its CI run and a staged diff, and show that there is no way to run a command. Approve a release-gate approval with Windows Hello (once Phase 19 adds it). Install an update from the internal channel and roll it back.

## Exit gate — Milestone M6

- [ ] All 43 screens on Windows and all 31 ALL screens on Android ([Phase 17](phase-17-android.md)), each with its states.
- [ ] PRD Appendix A scenarios 1 to 8 pass on the three clients.
- [ ] Signed release installed by the owner; update and rollback verified.
- [ ] Accessibility (Narrator, TalkBack, keyboard, WCAG 2.2 AA on the web) and RTL checks passed on all clients.
- [ ] The owner approved moving on.

## Rollback

Stop the staged rollout and point the update source back to the previous version; the web keeps every capability; server-side additions are behind flags.

## Risks

| Risk | Mitigation |
|---|---|
| WinUI RTL gaps | Spike first (18.1); documented workarounds; fall back to custom controls only where needed |
| Developer Console becomes a back door | Read-only by design; actions only through policy-checked API routes; static check in CI |
| Signing identity validation delays release | Start it in Phase 16; internal channel can use a test certificate on the owner's machine only |
| Three clients drift | No business logic in clients; generated API client; shared tokens and component states; parity tables |

## Cost impact

Windows code signing, about $10/month ([COST_MODEL](../../cost/COST_MODEL.md); verify the current price at kickoff). No model spend.
