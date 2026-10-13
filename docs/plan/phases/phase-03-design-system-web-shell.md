# Phase 3 — Design system in code and web shell

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-clients` (most work), `htco-platform` (BFF auth endpoints) |
| **Size** | L |
| **Depends on** | Phase 2 |
| **Requirements** | FR-ADM-01, FR-IAM-01, FR-IAM-02, FR-IAM-05, FR-IAM-06 |
| **Status** | Not started |

## Progress

- [ ] 3.1 Clients repository tooling
- [ ] 3.2 Token pipeline
- [ ] 3.3 Web app skeleton
- [ ] 3.4 Core components
- [ ] 3.5 BFF authentication (platform)
- [ ] 3.6 Generated API client and contract job
- [ ] 3.7 Overview and shops screens
- [ ] 3.8 Web deployment
- [ ] 3.9 Administration screens

## Goal

The owner signs in on `app.ht-co.app` with password and authenticator app, and lands in a calm, branded, bilingual shell that shows their organization and shops. Underneath: the token pipeline, the core component library with accessibility and RTL tests, generated API clients and a secure session model.

## Why now

Every later phase adds screens. If tokens, components, i18n, RTL, accessibility and auth patterns are not settled first, each screen invents its own and the product looks and behaves inconsistently.

## Scope

**In:** `htco-clients` tooling and CI; design tokens build (CSS, Kotlin and XAML outputs, only CSS consumed now); web app skeleton; core components with Storybook; Lingui (Hebrew and English) with RTL; light and dark themes; BFF auth in the API (sign in, MFA enroll and verify, sign out, session list and revoke); generated TypeScript API client; overview and shops screens; web deployment with security headers.

**Out:** Android and Windows apps (Phases 14–15); business screens beyond overview and shops.

## Owner prerequisites

- `app.ht-co.app` DNS ready (or use the default DigitalOcean domain until it is).
- Approved design system (Phase 0).

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

## Test plan

Vitest unit and component tests; Storybook interaction and axe tests; screenshot tests; Playwright journeys against MSW and against the local stack; API tests for the BFF; manual verification with the feature-verification skill (keyboard-only, screen reader spot check, RTL, dark mode, slow network).

## Security checklist

- [ ] ASVS V3 (web frontend), V7 (sessions), V6 (MFA): cookie attributes, CSRF, session revocation.
- [ ] CSP without `unsafe-inline` scripts; no third-party scripts.
- [ ] No tokens or PII in browser storage, URLs or analytics.

## Documentation

`htco-clients/docs/features/web-shell/` and `docs/features/auth-bff/` in the platform; design system docs updated with implemented components; ADR-0006 validation recorded.

## Demo

Owner signs in on the deployed site, enrolls MFA, switches Hebrew and English, light and dark, revokes a session from another browser.

## Exit gate

- [ ] All AC-3.x pass; axe clean; security headers verified on the deployed site.

## Rollback

Web and API deploys roll back independently; the sessions table is additive.

## Risks

| Risk | Mitigation |
|---|---|
| Tooling churn (TS 7, Vitest 5, Storybook 10, MSW 3 are all recent majors) | Exact pins; Renovate with release age; upgrade one at a time |
| Server-side PKCE exchange not supported by the Python Supabase client | Call Supabase Auth REST endpoints directly (A-11) |

## Cost impact

`web` service about $5/month if served by Caddy (static site would be free but cannot set security headers).
