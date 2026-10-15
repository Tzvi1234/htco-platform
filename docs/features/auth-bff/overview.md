# Web sign-in (BFF) — Overview

| | |
|---|---|
| **Slug** | `auth-bff` |
| **Phase / slices** | Phase 3 — slice 3.5 |
| **Requirements** | [FR-IAM-01, FR-IAM-02, FR-IAM-05, FR-IAM-06](../../product/PRD.md) |
| **Decision** | [ADR-0006](../../adr/0006-web-auth-bff.md) |
| **Status** | In progress (parts 1 and 2: sign-in, sessions, CSRF, MFA, step-up, session list, accepting invitation links; part 3: password reset at the API, waiting for the owner to connect Resend as Supabase's SMTP) |
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
8. Accepting an invitation: an Owner or Admin gets a one-time invitation link from `POST /v1/organizations/{id}/invitations` ([identity-access](../identity-access/overview.md#invitation-links); no email provider yet) and sends it to the invitee. The link lands on the web app's accept page (`HTCO_INVITE_REDIRECT_URL`) with Supabase's tokens in the URL fragment. The page posts `{"access_token", "refresh_token", "password"}` once to `POST /v1/auth/invitations/accept`, with the same CSRF header and `Origin` checks as sign-in. The API verifies the access token like a bearer token, sets the password as that user (`PUT /auth/v1/user`), exchanges the refresh token for fresh tokens, checks they belong to the same user and opens the session exactly like sign-in: same `SessionView` body (`user_id`, `assurance`), same cookie, `no-store`. The password is 12 to 128 characters (422 `validation-error`); one Supabase's policy refuses is 422 `weak-password`; an invalid, expired or used link is 401 `invitation-refused`. Refused links are counted per link and per client address in the sign-in attempt log (5 per link, 20 per address in 15 minutes, shared with sign-in failures), then 429 `too-many-attempts`.
9. Forgot password: the web app posts `{"email"}` to `POST /v1/auth/password/forgot` (same CSRF header and `Origin` checks as sign-in). The API counts the request atomically (`SessionStore.admit_attempt`, under advisory locks on the email key and the address, so a parallel burst cannot pass the limits; 5 per email and 20 per address in 15 minutes; the address limit is shared with failed sign-ins; reset requests never lock the email's sign-in), answers **202 with an empty body and `no-store`**, and only then asks Supabase Auth to email a recovery link (`POST /auth/v1/recover` with `{"email"}` and `redirect_to` = `HTCO_PASSWORD_RESET_REDIRECT_URL`). The answer and its timing are the same whether or not the account exists; Supabase's own errors (including its 429 and being unreachable) are logged as `auth.password_reset_email_failed`, without the email, and the answer stays 202. Only a malformed request (422), a CSRF failure (403), our own limit (429 `too-many-attempts` with `Retry-After`) or a missing redirect setting (503 `auth-unavailable`, a misconfiguration we do not hide) differ.
10. Reset password: the emailed link lands on the web app's reset page with a recovery session's tokens in the URL fragment. The page posts `{"access_token", "refresh_token", "password"}` (and `"code"`, see below) once to `POST /v1/auth/password/reset`. The mechanics are those of step 8, through one shared application function (`set_password_from_link`, with the throttle key namespaced `password-reset:` instead of `invitation:`): verify the token, set the password, exchange the refresh token, same-user check, open the session, same cookie and `SessionView`, `no-store`. An invalid, expired or used link is 401 `reset-link-refused`: before anything changes, the link is claimed atomically (a success row under its key in the attempt log, written under a Postgres advisory lock; `SessionStore.claim_link`), so a link that already opened a session in the last 24 hours, or a second request racing with the same link, is refused even while the access token is still valid. The claim is released only while the password is unchanged (weak or unchanged password, missing code, refused link before the change), so the page can try again; once Supabase changed the password the link stays claimed, even if the refresh then fails. This applies to invitation links too. A weak password is 422 `weak-password`, the current password 422 `same-password` (neither is counted); failures 429 as above. After the reset, **every other active session of the user is revoked**, here and at Supabase (`POST /auth/v1/logout?scope=local` with each session's token), because whoever knew the old password may hold one.
    - A user with an authenticator app: Supabase changes the password only from an `aal2` session (it answers `insufficient_aal`). The API then answers 403 `mfa-code-required` without counting a failure; the page asks for the 6-digit code and posts again with `"code"`. The API tries it against each of the user's verified factors (one may be the lost one; a wrong code counts once) with the recovery session (Supabase challenge and verify), sets the password with the resulting `aal2` session and opens the web session at `aal2`. A wrong code is 401 `mfa-code-refused`, counted against the link and against the user's code limit (5 wrong codes in 15 minutes, shared with step-up).

Email verification at sign-up is not needed: accounts are invite-only (an Owner or Admin invites, step 8), and the invitation link itself proves the address. There is no public sign-up.

## Acceptance criteria

| ID | Criterion | Part | Status |
|---|---|---|---|
| AC-3.5.1 | After sign-in, the browser holds only the session cookie; no token in any browser storage. | 1 | Pass at the API (no token in any response; the cookie is opaque); the end-to-end browser check comes with the web sign-in screen |
| AC-3.5.2 | An unsafe request without the CSRF header, or with a cross-site `Origin`, is rejected with 403. | 1 | Pass |
| AC-3.5.3 | An owner without MFA is forced into enrollment; owner-only calls refused at `aal1`. | 2 | API: pass (`mfa-required`; enrollment endpoints). The forced redirect to enrollment is the web app's, with its sign-in screens |
| AC-3.5.4 | Revoking a session from another device signs that device out on its next request. | 1, 2 | Pass (session list and revoke; revoked sessions and refused refresh tokens end on the next request) |
| AC-3.5.5 | Repeated failed sign-ins are rate-limited and audited without revealing whether the email exists. | 1 | Pass (5 failures per email or 20 per address in 15 minutes; hashed records in `private.sign_in_attempts`) |
| AC-3.5.6 | Password reset and email verification work end to end without revealing whether an account exists. | 3 | API: pass (same 202 for any email, reset like an invitation, other sessions revoked). Email verification is not needed (invite-only accounts). End to end needs Resend connected as Supabase's SMTP ([operations](operations.md#connecting-resend-for-auth-emails)) and the web pages ([htco-clients](https://github.com/Tzvi1234/htco-clients)) |
| AC-3.5.7 | Step-up re-authentication with a 10-minute elevation. | 2 | Pass (injected clock: required, allowed after the code, required again after ten minutes) |
| AC-3.5.8 | Recovery path for a lost authenticator. | 2, 3 | A second authenticator can be enrolled (the enrollment endpoints allow it); the owner-assisted, audited reset comes with part 3 |
| AC-3.5.9 | An invited user who opens the invitation link sets a password and is signed in with the same cookie as a sign-in; invalid links 401, weak passwords 422, failures throttled. | 2 | Pass at the API; the accept page comes with the web app's sign-in screens |

## Configuration

| Setting | Meaning |
|---|---|
| `HTCO_SESSION_KEY` | 32 random bytes, base64 (`openssl rand -base64 32`). Seals tokens at rest. A secret; rotating it ends every session. |
| `HTCO_SUPABASE_PUBLISHABLE_KEY` | The project's publishable key (`sb_publishable_…`), sent to Supabase Auth. Not secret. |
| `HTCO_WEB_ORIGINS` | Comma-separated origins allowed to send cookie requests, e.g. `https://dash.ht-co.app`. |
| `HTCO_INVITE_REDIRECT_URL` | Where invitation links land: the web app's accept page (`https://dash.ht-co.app/accept-invite` in production, set in the app spec). Must be in Supabase Auth's redirect allow-list. |
| `HTCO_PASSWORD_RESET_REDIRECT_URL` | Where password reset links land: the web app's reset page (`https://dash.ht-co.app/reset-password` in production, set in the app spec). Must be in Supabase Auth's redirect allow-list. Without it, `POST /v1/auth/password/forgot` answers 503. |

Web sign-in is on when the two keys are set (with the Supabase and database URLs); `api.starting` logs `web_sign_in`.

## Facts learned about Supabase Auth

- `POST /auth/v1/recover` with `{"email"}` and the `redirect_to` query parameter sends the recovery email; the link redirects to `redirect_to` with the session's tokens and `type=recovery` in the URL fragment (implicit flow), or with `error` and `error_code` (for example `otp_expired`) when it is no longer valid.
- Updating the password (`PUT /auth/v1/user`) of a user with a verified authenticator app needs an `aal2` session; from an `aal1` (recovery) session Supabase answers 401 `insufficient_aal` (Supabase Auth `user.go`, read 2026-10-15; not yet seen live).
- Until custom SMTP is configured, Supabase's built-in mailer delivers only to the project team's addresses and is rate limited; the API code does not depend on the provider.
