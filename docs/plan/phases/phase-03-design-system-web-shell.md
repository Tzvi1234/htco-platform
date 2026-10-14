# Phase 3 — Design system in code and web shell

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-clients` (most work), `htco-platform` (BFF auth endpoints) |
| **Size** | L |
| **Depends on** | [Phase 2](phase-02-identity-tenancy.md) (slices 3.5–3.9); slice 3.10 depends only on [Phase 0](phase-00-groundwork.md) |
| **Requirements** | FR-ADM-01, FR-IAM-01, FR-IAM-02, FR-IAM-05, FR-IAM-06 |
| **Mandatory tests owned** ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | None |
| **Status** | In progress: 3.1, 3.2 and 3.3 (part 1) in htco-clients pull requests #1–#4 |

## Progress

- [ ] 3.1 Clients repository tooling
- [x] 3.2 Token pipeline
- [ ] 3.3 Web app skeleton
- [ ] 3.4 Core components
- [ ] 3.5 BFF authentication (platform)
- [ ] 3.6 Generated API client and contract job
- [ ] 3.7 Overview and shops screens
- [ ] 3.8 Web deployment
- [ ] 3.9 Administration screens
- [ ] 3.10 Design system v2 inventory (DS0)

Slice 3.10 (DS0) is design work, not screens in code. It can run in parallel with every other slice and must be approved before the first office screens are built in [Phase 5](phase-05-tasks-messages-approvals.md) and [Phase 7](phase-07-pilot-office.md).

Slices 3.1 to 3.3 need no backend, so they started early (the plan lets design-system work overlap Phase 2). Slice 3.1 ships in two parts: workspace tooling and CI first; the logical-CSS lint rule with the web app (3.3), and Storybook with axe with the core components (3.4). Slice 3.3 ships in two parts: the shell, languages, themes and states first ([htco-clients#4](https://github.com/Tzvi1234/htco-clients/pull/4)); the Playwright journeys with axe and screenshots second. The token pipeline's `HighContrast` XAML dictionary moves to Phase 18, with the Windows app ([client ADR-0003](https://github.com/Tzvi1234/htco-clients/blob/main/docs/adr/0003-design-token-pipeline.md)).

## Goal

The owner signs in on `app.ht-co.app` with password and authenticator app, and lands in a calm, branded, bilingual shell that shows their organization and shops. Underneath: the token pipeline, the core component library with accessibility and RTL tests, generated API clients and a secure session model. Alongside it, the design system v2 inventory (stage DS0 of the clients specification) fixes every component, screen and navigation path the office will need, so later phases build screens instead of inventing them.

## Why now

Every later phase adds screens. If tokens, components, i18n, RTL, accessibility and auth patterns are not settled first, each screen invents its own and the product looks and behaves inconsistently.

## Scope

**In:** `htco-clients` tooling and CI; design tokens build (CSS, Kotlin and XAML outputs, only CSS consumed now); web app skeleton; core components with Storybook; Lingui (Hebrew and English) with RTL; light and dark themes; BFF auth in the API (sign in, MFA enroll and verify, sign out, session list and revoke); generated TypeScript API client; overview and shops screens; web deployment with security headers.

**In (DS0):** the cross-platform component inventory of the clients specification, the 43-screen specification, the navigation information architecture and a clickthrough prototype, with Mobbin references.

**Out:** Android and Windows apps ([Phases 17](phase-17-android.md) and [18](phase-18-windows.md)); business screens in code beyond overview and shops (DS0 designs them; each is built in the phase that owns its data, and all 43 on the web in [Phase 16](phase-16-web-office.md)).

## Owner prerequisites

- `app.ht-co.app` DNS ready (or use the default DigitalOcean domain until it is).
- Approved design system ([Phase 0](phase-00-groundwork.md)).
- For slice 3.10: time for one design review of DS0 (about an hour), and access to the Mobbin references the agent collects.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification`, `feature-docs` and, in `htco-clients`, `design-system`.
2. Read this file, [PLAN.md](../PLAN.md) §1 and §6, the clients `CLAUDE.md`, `htco-clients/docs/ux/` (navigation map, accessibility, content style), `htco-clients/design/COMPONENT_SPEC.md`, the clients ADRs (three native clients, web stack, token pipeline, API client generation, brand palette) and, for 3.5, [ADR-0006](../../adr/0006-web-auth-bff.md).
3. Do not redo the work in pull requests #1–#4 (3.1, 3.2, 3.3 part 1); continue from there. Order: 3.3 part 2, 3.4, 3.6, then 3.5 (needs [Phase 2](phase-02-identity-tenancy.md) slices 2.1–2.3), 3.7, 3.8, 3.9. Slice 3.10 runs in parallel and needs no backend.
4. One pull request per slice; acceptance criteria first as failing tests (Vitest, Storybook interaction, axe, Playwright). For 3.10, the acceptance criteria are checked by the owner's design review and by a link and coverage check over the documents.
5. For every screen and component: Hebrew RTL and English LTR, light and dark, keyboard and screen reader, reduced motion, and the loading, empty, error, offline and no-permission states. Agent statuses are the eight real states only; never invent a status or an animation that is not backed by data.
6. Never in this phase: store a token or personal data in browser storage; add a third-party script; use physical CSS directions; recreate or modify the official logo; show Jev or GPT image generation as an employee; design a control that would let a user switch an employee to a personal subscription ([ADR-0017](../../adr/0017-auth-and-billing-policy.md)).
7. Stop and ask the owner when: a screen in the clients specification is ambiguous or conflicts with the PRD; a Mobbin reference suggests a pattern that changes the navigation; the design review asks for changes to tokens approved in Phase 0.
8. Finish with the feature-verification run (keyboard-only, screen reader spot check, RTL, dark mode, slow network), the feature docs and the PLAN status table.

## Slices

### 3.1 — Clients repository tooling

pnpm workspace; TypeScript strict (compiler TS 7 with a TS 6 alias for ESLint tooling until typescript-eslint supports TS 7); ESLint with a rule that enforces logical CSS properties; Prettier; Vitest; Playwright; Storybook 10 with the accessibility addon; CI with path filters, caching and one required `ci-gate`; Renovate.

| ID | Acceptance criterion |
|---|---|
| AC-3.1.1 | A physical direction utility (`ml-4`, `left-0`) in a component fails lint. |
| AC-3.1.2 | A failing Vitest, Storybook interaction or axe check fails `ci-gate`. |

### 3.2 — Token pipeline

DTCG source in `design/tokens/` built with Style Dictionary into CSS custom properties (light, dark, `prefers-color-scheme` fallback), Kotlin objects and a XAML `ResourceDictionary` with theme dictionaries; a contrast test over every declared text and control pair; generated files committed and checked for drift.

| ID | Acceptance criterion |
|---|---|
| AC-3.2.1 | Changing a token value without regenerating outputs fails CI. |
| AC-3.2.2 | A token pair below WCAG AA (4.5:1 text, 3:1 controls) fails the contrast test. |

### 3.3 — Web app skeleton

Vite + React + TanStack Router and Query; app shell (sidebar, top bar, shop switcher, user menu); Lingui catalogs for Hebrew and English with extraction check; `dir` and `lang` switch on the root; theme switch (system, light, dark) remembered per device; error boundary; offline and stale banners.

| ID | Acceptance criterion |
|---|---|
| AC-3.3.1 | Switching to Hebrew sets `dir="rtl"` and mirrors the layout; English restores LTR. |
| AC-3.3.2 | A user-visible string missing from a catalog fails the i18n check. |
| AC-3.3.3 | Theme follows the system by default and the user's choice when set. |

### 3.4 — Core components

Button, IconButton, Input, PasswordField, SearchField, Select, Badge, StatusDot, MetricCard, Skeleton, EmptyState, ErrorState, RetryBanner, Toast, Dialog, Sidebar, TopBar, FilterChips, Pagination, `Ltr` (bidi isolation for codes); built on React Aria Components; every component has stories for each state, light and dark, LTR and RTL.

| ID | Acceptance criterion |
|---|---|
| AC-3.4.1 | Every component story passes axe with zero serious or critical violations. |
| AC-3.4.2 | Every interactive component is fully operable by keyboard, with a visible focus ring (interaction tests). |
| AC-3.4.3 | Screenshot tests exist for he/en × light/dark for each component. |
| AC-3.4.4 | A tracking number inside Hebrew text renders in the correct order (`Ltr` test). |

### 3.5 — BFF authentication (platform)

Endpoints under `/v1/auth/*`: sign in (password), MFA enroll (TOTP QR), MFA verify, sign out, list and revoke sessions, password reset, email verification, MFA recovery (a second enrolled factor, or an owner-assisted reset that is audited), step-up re-authentication (`POST /v1/auth/step-up`: re-enter TOTP; the session counts as recently re-authenticated for 10 minutes), and a record of sign-ins from new devices; `web_sessions` table with encrypted Supabase tokens; `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`); CSRF header and `Origin`/`Sec-Fetch-Site` checks on unsafe methods; refresh handled server-side; brute-force protection.

| ID | Acceptance criterion |
|---|---|
| AC-3.5.1 | After sign-in, the browser holds only the session cookie; no token appears in local storage, session storage or IndexedDB (end-to-end check). |
| AC-3.5.2 | An unsafe request without the CSRF header, or with a cross-site `Origin`, is rejected with 403. |
| AC-3.5.3 | An owner without MFA is forced into enrollment before any owner-only screen; the API refuses owner-only calls at `aal1`. |
| AC-3.5.4 | Revoking a session from another device signs that device out on its next request. |
| AC-3.5.5 | Repeated failed sign-ins are rate-limited and audited without revealing whether the email exists. |
| AC-3.5.6 | Password reset and email verification work end to end and never reveal whether an account exists. |
| AC-3.5.7 | An action that requires recent re-authentication returns 403 with a `step-up-required` problem type until the user re-enters TOTP; the elevation expires after 10 minutes (tested with an injected clock). |
| AC-3.5.8 | The documented recovery path for a lost authenticator works (second factor, or owner-assisted reset with an audit record). |

### 3.6 — Generated API client and contract job

Pinned copy of the platform's `openapi.yaml` in `packages/api-clients`; openapi-typescript + openapi-fetch + openapi-react-query; typed MSW handlers (openapi-msw); a job that regenerates the client and fails on drift.

| ID | Acceptance criterion |
|---|---|
| AC-3.6.1 | Updating the pinned spec without regenerating fails CI; a removed field breaks compilation where it is used. |

### 3.7 — Overview and shops screens

Overview with empty state ("Connect your first shop" for the owner), organization and role, shops list from `/v1/shops`; loading, empty, error, stale and no-permission states.

| ID | Acceptance criterion |
|---|---|
| AC-3.7.1 | Each state matches its approved screenshot baseline in he/en × light/dark. |
| AC-3.7.2 | A Viewer sees no administrative controls, and the API refuses them regardless. |

### 3.8 — Web deployment

`web` component: Caddy serving the built app with CSP (no inline scripts), HSTS, `frame-ancestors 'none'`, `Referrer-Policy`, `Permissions-Policy`; ingress routes `/api` to the API; custom domain when ready.

| ID | Acceptance criterion |
|---|---|
| AC-3.8.1 | Security headers present on every web response (automated check against the deployed site). |
| AC-3.8.2 | The deployed app passes the Playwright sign-in journey (with a test user on the deployed environment). |

### 3.9 — Administration screens

Users and roles, shop access and sessions, built on the slice 2.6 endpoints.

| ID | Acceptance criterion |
|---|---|
| AC-3.9.1 | The owner can invite a user, set the role, grant a shop and see each change in the audit view, all from the web app. |
| AC-3.9.2 | Non-admin roles do not see the Administration section, and its routes show the no-permission state (the API refuses anyway). |

### 3.10 — Design system v2 inventory (DS0)

Stage DS0 of the clients specification's release roadmap: brand tokens (from Phase 0, unchanged unless the review asks), the cross-platform component inventory, the navigation information architecture and a clickthrough prototype. The output is design documentation and a prototype in `htco-clients`, not production screens.

**Component inventory.** Each component is specified once for web, Android and Windows: purpose, anatomy, variants, states (loading, empty, error, offline, disabled, no permission), the data it shows and where that data comes from, accessibility (role, keyboard, focus, screen reader text), RTL behaviour, motion and reduced motion, and the phase that first builds it.

| Component | Notes |
|---|---|
| `AppShell` | Sidebar, top bar, content; responsive; Android bottom navigation (Home, Office, Tasks, Inbox, More) and the Windows three-column layout as platform variants |
| `CommandPalette` | Global search and commands; never offers a command the caller lacks the capability for |
| `Avatar` | Person or employee; employees are clearly AI, never shown as people |
| `EmployeeCard` | Identity, department, `AgentStatus`, current task; opens the employee profile |
| `DepartmentCard` | Seven people departments and three service departments (Automations, AI Tools, Infrastructure), visibly distinct |
| `AgentStatus` | Exactly eight states: IDLE, QUEUED, RUNNING, WAITING_EXTERNAL, WAITING_APPROVAL, BLOCKED, FAILED, OFFLINE, derived from task runs and the task state machine; colour plus text plus icon, never colour alone |
| `TaskCard` | Title, owner, shop, priority, deadline, state, evidence indicator |
| `MessageThread` | User to employee, employee to employee, employee to department and employee to task threads; delivery and acknowledgement receipts; structured attachments |
| `ToolResult` | A tool call's outcome as data (tool, decision, result summary, links), never model reasoning |
| `ApprovalSheet` | What will happen, why, risk, cost, who asked; approve or reject, with step-up where required |
| `AutomationExecutionRow` | Trigger ID, idempotency key, attempt, outcome, trace link |
| `PolicyChip` | A policy decision or requirement (allowed, denied, needs approval) with its reason |
| `RoutingTrace` | Chosen runtime and model, fallback, actual cost; no chain of thought |
| `ModelSelector` | Only catalog models allowed by the employee's policy; shows the verification date |
| `CostChart` | API cost separate from credits and from informational subscription usage; never an invented per-token cost |
| `IncidentCard` | Severity, owner, state, evidence, linked order or task |
| `ShopSwitcher` | Multi-shop context; only granted shops |
| `OrderTimeline` | Order state transitions with source and time; tracking and sync proof |
| `CatalogAssetGrid` | Images with provenance and review state (`NEEDS_QA` visible) |
| `BillingModeBadge` | The run's `billing_mode` (API, credit, and an approved subscription mode only if one ever exists), with data provenance; informational, never a switch |

**Screen specification.** `htco-clients/docs/ux/SCREEN_SPEC.md` lists all 43 screens of the clients specification's screen matrix (from Welcome and sign-in to Preferences, offline and error states), each with: primary data and actions, the API it needs, platform priority (all platforms, or web and Windows), states, roles allowed, the components it uses, the phase that builds it, and its UX stage (UX1–UX6).

**Navigation and prototype.** The navigation information architecture of the specification (Authentication, Home, Office, Work, Commerce, Products, Finance, Marketing, Customer Service, System, AI Operations, Engineering, Mail, Admin) in `htco-clients/docs/ux/NAVIGATION_MAP.md`, updated to the version 3.0 phase numbers; a clickthrough prototype covering the main journeys (sign in, home, office to employee profile, task to approval, order detail, automation execution log, model router console, AI cost and quota). **Mobbin references:** for each main pattern (dashboard, kanban, chat thread, approval sheet, data table, timeline, settings), two or three references from Mobbin, each with a note on what is taken from it.

| ID | Acceptance criterion |
|---|---|
| AC-3.10.1 | Every component in the table above is specified in the design system docs with all the listed fields; a check fails if a component in the inventory has no specification or no states. |
| AC-3.10.2 | `AgentStatus` defines exactly the eight states and their mapping from task and agent-run states; no other status appears in any screen or in the prototype. |
| AC-3.10.3 | `SCREEN_SPEC.md` contains all 43 screens, each with every field above; a coverage check lists none missing. |
| AC-3.10.4 | The navigation map and prototype reach every screen of the specification, show all 16 employees in their seven departments and the three service departments as distinct from people, and place Jev in AI Operations and GPT images in Nika's Creative Studio (the clients specification's anti-omission checklist). |
| AC-3.10.5 | Each main pattern has its Mobbin references recorded with the reason for the choice. |
| AC-3.10.6 | **Exit: owner design review.** The owner reviewed the inventory, the screen specification and the prototype; the decision and requested changes are recorded with the date. |

## Test plan

Vitest unit and component tests; Storybook interaction and axe tests; screenshot tests; Playwright journeys against MSW and against the local stack; API tests for the BFF; manual verification with the feature-verification skill (keyboard-only, screen reader spot check, RTL, dark mode, slow network).

## Security checklist

- [ ] ASVS V3 (web frontend), V7 (sessions), V6 (MFA): cookie attributes, CSRF, session revocation.
- [ ] CSP without `unsafe-inline` scripts; no third-party scripts.
- [ ] No tokens or PII in browser storage, URLs or analytics.

## Documentation

`htco-clients/docs/features/web-shell/` and `docs/features/auth-bff/` in the platform; `htco-clients/docs/ux/SCREEN_SPEC.md`, the updated `NAVIGATION_MAP.md` and the component specifications (DS0); design system docs updated with implemented components; ADR-0006 validation recorded.

## Demo

Owner signs in on the deployed site, enrolls MFA, switches Hebrew and English, light and dark, revokes a session from another browser. Then the DS0 review: walk the component inventory, the 43-screen specification and the clickthrough prototype.

## Exit gate

- [ ] All AC-3.x pass; axe clean; security headers verified on the deployed site.
- [ ] DS0 approved in the owner's design review (AC-3.10.6).

## Rollback

Web and API deploys roll back independently; the sessions table is additive.

## Risks

| Risk | Mitigation |
|---|---|
| Tooling churn (TS 7, Vitest 5, Storybook 10, MSW 3 are all recent majors) | Exact pins; Renovate with release age; upgrade one at a time |
| Server-side PKCE exchange not supported by the Python Supabase client | Call Supabase Auth REST endpoints directly (A-11) |

## Cost impact

`web` service about $5/month if served by Caddy (static site would be free but cannot set security headers).
