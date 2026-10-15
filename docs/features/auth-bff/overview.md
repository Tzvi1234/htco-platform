# Web sign-in (BFF) — Overview

| | |
|---|---|
| **Slug** | `auth-bff` |
| **Phase / slices** | Phase 3 — slice 3.5 |
| **Requirements** | [FR-IAM-01, FR-IAM-02, FR-IAM-05, FR-IAM-06](../../product/PRD.md) |
| **Decision** | [ADR-0006](../../adr/0006-web-auth-bff.md) |
| **Status** | In progress (parts 1 and 2: sign-in, sessions, CSRF, MFA, step-up, session list; part 3: email flows) |
| **Last updated** | 2026-10-15 |

## What and why

The web app signs in through the API, never directly with Supabase. The API checks the password with Supabase Auth, keeps the user's tokens on the server (sealed with AES-256-GCM, bound to the session) and gives the browser one opaque cookie. A cross-site scripting bug in the web app can therefore never read a token, and the server can end any session at once.

## Flow

1. `POST /v1/auth/sign-in` with email and password, the `X-HTCO-CSRF` header and the web app's `Origin`. The API throttles failed attempts per email and per client address, asks Supabase Auth (password grant), verifies the access token like a bearer token, stores the session and sets `__Host-htco_session` (`Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`).
2. Every later request with the cookie resumes the session: known, not revoked, inside its idle (12 hours, extended by activity) and absolute (14 days) lifetimes. An access token within two minutes of expiry is refreshed on the server. Unsafe methods must pass the CSRF checks.
3. `CurrentAuth` accepts either a bearer token (native apps) or the cookie (web); memberships are read from the database either way.
4. `POST /v1/auth/sign-out` revokes the session here and at Supabase and clears the cookie. `GET /v1/auth/session` says who is signed in.
5. MFA: `GET /v1/auth/mfa` (assurance and authenticator apps), `POST /v1/auth/mfa/enroll` (QR code and secret, `no-store`; any unconfirmed authenticator from an earlier attempt is removed first, since Supabase refuses a second factor with the same name, and a name already used by a verified one gets a number), `POST /v1/auth/mfa/verify` (first code: the session's tokens are replaced by Supabase's `aal2` tokens and the moment counts as a re-authentication). Wrong codes are throttled like passwords (5 per user in 15 minutes).
6. Step-up: `POST /v1/auth/step-up` re-enters a code; for ten minutes the policy engine treats the session as recently re-authenticated (`step-up-required` otherwise). `require_capability` in `htco_api.policy_guard` answers `forbidden`, `mfa-required` or `step-up-required`.
7. Devices: `GET /v1/auth/sessions` lists the user's active sessions (`current` marks this browser); `DELETE /v1/auth/sessions/{id}` ends one here and at Supabase.

## Acceptance criteria

| ID | Criterion | Part | Status |
|---|---|---|---|
| AC-3.5.1 | After sign-in, the browser holds only the session cookie; no token in any browser storage. | 1 | Pass at the API (no token in any response; the cookie is opaque); the end-to-end browser check comes with the web sign-in screen |
| AC-3.5.2 | An unsafe request without the CSRF header, or with a cross-site `Origin`, is rejected with 403. | 1 | Pass |
| AC-3.5.3 | An owner without MFA is forced into enrollment; owner-only calls refused at `aal1`. | 2 | API: pass (`mfa-required`; enrollment endpoints). The forced redirect to enrollment is the web app's, with its sign-in screens |
| AC-3.5.4 | Revoking a session from another device signs that device out on its next request. | 1, 2 | Pass (session list and revoke; revoked sessions and refused refresh tokens end on the next request) |
| AC-3.5.5 | Repeated failed sign-ins are rate-limited and audited without revealing whether the email exists. | 1 | Pass (5 failures per email or 20 per address in 15 minutes; hashed records in `private.sign_in_attempts`) |
| AC-3.5.6 | Password reset and email verification work end to end without revealing whether an account exists. | 3 | Open (needs an email provider) |
| AC-3.5.7 | Step-up re-authentication with a 10-minute elevation. | 2 | Pass (injected clock: required, allowed after the code, required again after ten minutes) |
| AC-3.5.8 | Recovery path for a lost authenticator. | 2, 3 | A second authenticator can be enrolled (the enrollment endpoints allow it); the owner-assisted, audited reset comes with part 3 |

## Configuration

| Setting | Meaning |
|---|---|
| `HTCO_SESSION_KEY` | 32 random bytes, base64 (`openssl rand -base64 32`). Seals tokens at rest. A secret; rotating it ends every session. |
| `HTCO_SUPABASE_PUBLISHABLE_KEY` | The project's publishable key (`sb_publishable_…`), sent to Supabase Auth. Not secret. |
| `HTCO_WEB_ORIGINS` | Comma-separated origins allowed to send cookie requests, e.g. `https://dash.ht-co.app`. |

Web sign-in is on when the two keys are set (with the Supabase and database URLs); `api.starting` logs `web_sign_in`.
