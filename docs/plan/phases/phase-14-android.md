# Phase 14 — Android app

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-clients` |
| **Size** | L |
| **Depends on** | Phase 7 (can run in parallel with Phases 8–13) |
| **Requirements** | FR-IAM-06, FR-NTF-02 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 14.1 Project setup: Gradle catalog, Hilt, Retrofit + kotlinx.serialization, ktlint, detekt (advisory), JUnit, Turbine, Roborazzi, CI
- [ ] 14.2 Design system in Compose from generated tokens
- [ ] 14.3 Sign-in per the auth ADR, secure storage, logout clears everything
- [ ] 14.4 Approvals and push deep links; biometric step-up
- [ ] 14.5 Incidents and overview
- [ ] 14.6 Release pipeline to internal testing; baseline profiles

## Goal

The owner handles what needs them from the phone: approvals with push notifications and biometric confirmation, incidents, a daily summary and the dashboard, in a native Kotlin and Jetpack Compose app that looks like HTCO and behaves like Android.

## Scope

**In:** Compose app shell with tokens generated from the design system; sign-in decided by an ADR at kickoff: native screens calling Supabase Auth, or the HTCO web sign-in page in the system browser through Supabase's OAuth 2.1 server with PKCE, Auth Tab and verified App Links (A-16); tokens in DataStore encrypted with Tink and a Keystore-wrapped key; generated Kotlin API client; approvals inbox and approval card; biometric step-up for high-risk approvals: a Keystore key pair that requires biometrics is registered per device, the server sends a challenge and verifies the signature before accepting the decision; FCM push with deep links; incidents list and detail; overview; Hebrew and English with RTL; light and dark; TalkBack; release through Google Play internal testing.

**Out:** office canvas (grid or list only), finance exports, developer console.

## Owner prerequisites

- Google Play developer account; Firebase project for FCM; app signing decisions.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 14.1 | Project setup: Gradle catalog, Hilt, Retrofit + kotlinx.serialization, ktlint, detekt (advisory), JUnit, Turbine, Roborazzi, CI | `ci-gate` green; screenshot tests he/en × light/dark |
| 14.2 | Design system in Compose from generated tokens | Component previews and screenshot tests match the web's states |
| 14.3 | Sign-in per the auth ADR, secure storage, logout clears everything | No token outside encrypted storage; revocation honored |
| 14.4 | Approvals and push deep links; biometric step-up | TalkBack reads reason, impact and risk; expired approvals disabled; the server rejects a decision whose device signature is missing or invalid |
| 14.5 | Incidents and overview | Offline shows cached, labeled stale data only |
| 14.6 | Release pipeline to internal testing; baseline profiles | Cold start under 1.5 s on a mid-range device |

## Exit gate

- [ ] Owner uses the app for a week of real approvals; security checklist for mobile (deep links, screenshots of PII, clipboard, push payloads) passed.
