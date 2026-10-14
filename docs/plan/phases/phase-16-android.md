# Phase 16 — Android app

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-clients` |
| **Size** | L |
| **Depends on** | [Phase 7](phase-07-tasks-approvals.md) (can run in parallel with Phases 8–15) |
| **Requirements** | FR-IAM-06, FR-NTF-02 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 16.1 Project setup: Gradle catalog, Hilt, Retrofit + kotlinx.serialization, ktlint, detekt (advisory), JUnit, Turbine, Roborazzi, CI
- [ ] 16.2 Design system in Compose from generated tokens
- [ ] 16.3 Sign-in per the auth ADR, secure storage, logout clears everything
- [ ] 16.4 Approvals and push deep links; biometric step-up
- [ ] 16.5 Incidents, overview and office summary
- [ ] 16.6 Release pipeline to internal testing; baseline profiles

## Goal

The owner handles what needs them from the phone: approvals with push notifications and biometric confirmation, incidents, a daily summary, and a compact office summary (which employees and automations are working, waiting or blocked, from recorded activity), in a native Kotlin and Jetpack Compose app that looks like HTCO and behaves like Android.

## Scope

**In:** Compose app shell with tokens generated from the design system; sign-in decided by an ADR at kickoff: native screens calling Supabase Auth, or the HTCO web sign-in page in the system browser through Supabase's OAuth 2.1 server with PKCE, Auth Tab and verified App Links (A-16); tokens in DataStore encrypted with Tink and a Keystore-wrapped key; generated Kotlin API client; approvals inbox and approval card (including approvals requested by employees, with the requesting employee, impact and risk); biometric step-up for high-risk approvals: a Keystore key pair that requires biometrics is registered per device, the server sends a challenge and verifies the signature before accepting the decision; FCM push with deep links; incidents list and detail; overview and daily report; office summary as a list (states from the API, which derives them from recorded activity); Hebrew and English with RTL; light and dark; TalkBack; release through Google Play internal testing.

**Out:** the office canvas (list only), finance exports, the Engineering Console, chat with employees (a later iteration after [Phase 15](phase-15-office-executive.md)).

## Owner prerequisites

- Google Play developer account; Firebase project for FCM; app signing decisions.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 16.1 | Project setup: Gradle catalog, Hilt, Retrofit + kotlinx.serialization, ktlint, detekt (advisory), JUnit, Turbine, Roborazzi, CI | `ci-gate` green; screenshot tests he/en × light/dark |
| 16.2 | Design system in Compose from generated tokens | Component previews and screenshot tests match the web's states |
| 16.3 | Sign-in per the auth ADR, secure storage, logout clears everything | No token outside encrypted storage; revocation honoured |
| 16.4 | Approvals and push deep links; biometric step-up | TalkBack reads requester, reason, impact and risk; expired approvals disabled; the server rejects a decision whose device signature is missing or invalid |
| 16.5 | Incidents, overview and office summary | Offline shows cached, labelled stale data only; no employee shown as running when the worker is offline |
| 16.6 | Release pipeline to internal testing; baseline profiles | Cold start under 1.5 s on a mid-range device |

## Security checklist

- [ ] Mobile checklist: deep links, screenshots of PII, clipboard, push payloads without PII.
- [ ] Device keys verified by the server for step-up; no decision without a valid signature.

## Exit gate

- [ ] The owner uses the app for a week of real approvals; the mobile security checklist passed.

## Cost impact

Google Play developer account ($25 one-time, [COST_MODEL](../../cost/COST_MODEL.md)); FCM free tier.
