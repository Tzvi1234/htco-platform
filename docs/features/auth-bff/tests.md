# Web sign-in (BFF) — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Platform | Sealing: round trip, fresh nonce, wrong context, tampering, wrong key, malformed blobs, key validation, hidden key | `tests/platform/test_crypto.py` |
| Platform | Supabase Auth client: password and refresh grants, expiry from `expires_at` or `expires_in`, refused credentials, rate limit, service failures, sign-out of this session only, hidden tokens | `tests/platform/test_supabase_auth.py` |
| Application | Sign-in (hashed cookie, lifetimes, refusal without revealing the email, throttling per email and per address, bursts at once held to the limits, an unreachable provider not counted, a cancelled request still counted, window expiry), resume (fresh, refresh near expiry, refresh refused, idle and absolute expiry, activity extension, unknown and revoked cookies), sign-out | `tests/application/test_web_sessions.py` (fakes in `tests/session_fakes.py`) |
| API | Cookie attributes and no token in the response, cookie authenticates `/v1/me`, wrong password, 429 with `Retry-After`, CSRF on sign-in (login CSRF) and on cookie requests, sign-out, revoked elsewhere, bearer still works, not configured | `tests/api/test_web_auth.py` |
| API | Settings (origins, session key validation) and composition (web sign-in on only with both keys) | `tests/api/test_settings.py`, `tests/api/test_invitation_wiring.py` |
| Platform | Supabase MFA calls: enroll (QR, secret), challenge then verify (`aal2` tokens), wrong or expired codes, factor list, unenroll | `tests/platform/test_supabase_auth.py` |
| Application | Enroll and verify (tokens replaced, step-up time), wrong codes throttled (a burst at once held to the limit), a right code recorded as a success, status, step-up with the verified factor, no factor, the policy's ten-minute window, session list and revoke (own only) | `tests/application/test_mfa.py` |
| Platform | Setting the password as the user (`PUT /auth/v1/user`), `weak_password`, ended sessions refused, service failures | `tests/platform/test_supabase_auth.py` |
| Application | Accepting an invitation: password set, link's refresh token exchanged, session opened like sign-in; invalid or revoked link, refused refresh token, tokens of another user, weak password (not counted); throttling per address (shared with sign-in) and per link, a burst with one bad link held to the limit | `tests/application/test_web_sessions.py` |
| API | `POST /v1/auth/invitations/accept`: cookie and body like sign-in, `no-store`, the cookie authenticates `/v1/me`, 401 `invitation-refused`, 422 `weak-password` and length validation, CSRF 403, 429 with `Retry-After`, 503 when not configured; the composition root's error mapping | `tests/api/test_web_invitations.py`, `tests/api/test_invitation_wiring.py` |
| API | MFA status, enroll (`no-store`), verify (`aal2`), wrong code, step-up required then allowed then lapsed (injected clock), `mfa-required` at `aal1`, step-up without an authenticator, session list and revoke, session required | `tests/api/test_web_mfa.py` |
| Platform | Password reset email (`POST /auth/v1/recover` with `redirect_to`, publishable key only), its 429, refusals, failures and unreachable service (never carrying the email); `insufficient_aal` on a password change as `MfaRequiredError` (still an `AuthServiceError` for other callers) | `tests/platform/test_supabase_auth.py` |
| Application | Forgot password: email normalised, every request counted under its own key, 5 per email from any address, allowed again after the window, the address limit shared with failed sign-ins, reset requests never lock sign-in, provider failure reported as not sent. Reset: password set and session opened like an invitation, every other session of the user revoked here and at Supabase (other users' kept), invalid or used link refused without any change, weak password not counted and nothing revoked, per-link and per-address throttling, the `password-reset:` key namespace, authenticator users asked for a code (not counted), the code verified and the password set with the `aal2` session, wrong codes counted for the link and the user, the user's code limit (also for a burst over many links), a burst with one bad link held to the limit, a code without an authenticator ignored | `tests/application/test_password_reset.py` |
| API | `POST /v1/auth/password/forgot`: 202 with no body and `no-store` for known and unknown emails alike, Supabase failure still 202 and logged without the email, 429 per email with `Retry-After`, 422 for malformed bodies, CSRF 403, 503 without the redirect setting or web sign-in. `POST /v1/auth/password/reset`: cookie and body like sign-in, other browsers signed out, 401 `reset-link-refused`, 422 `weak-password` and validation, CSRF 403, 429, 403 `mfa-code-required` then 401 `mfa-code-refused` then 200 with the code, 503 not configured; settings and composition (`HTCO_PASSWORD_RESET_REDIRECT_URL`), the composition root's error mapping, the app spec value | `tests/api/test_web_password_reset.py`, `tests/api/test_invitation_wiring.py`, `tests/api/test_settings.py`, `tests/infra/test_app_spec.py` |
| Database (pgTAP) | Session and attempt tables private: RLS on, no privilege for `anon`, `authenticated` or `service_role`, tokens stored as bytes, unique cookie hash, sessions belong to real users | `supabase/tests/database/04_web_sessions.test.sql` |
| Integration (`-m db`) | Session store round trip with tokens sealed at rest, token replacement, touch, revoke, attempt counting per email and per address, concurrent reservations held to the key and address limits, settling (failure kept, success and not counted free their place), a concurrent sign-in burst against the real attempt log | `tests/integration/test_session_store.py` |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-3.5.1 | `test_sign_in__sets_only_a_host_only_secure_http_only_strict_cookie`, `test_create_find__round_trips_with_tokens_sealed_at_rest` |
| AC-3.5.2 | `test_sign_in__fails_csrf_checks__403`, `test_unsafe_cookie_request__without_csrf_header__403` |
| AC-3.5.4 | `test_sessions__list_and_revoke_another_device`, `test_revoke_session__another_device__signed_out_there_too`, `test_revoked_elsewhere__next_request_is_401_and_clears_the_cookie`, `test_resume__refresh_refused__ends_the_session`, `test_resume__revoked_session__ended` |
| AC-3.5.3 | `test_owner_without_mfa__mfa_required_for_owner_actions`, `test_enroll_then_verify__session_becomes_aal2` |
| AC-3.5.7 | `test_step_up_required__until_the_code_is_reentered_and_for_ten_minutes`, `test_step_up_window__the_policy_requires_it_and_it_expires_after_ten_minutes` |
| AC-3.5.9 | `test_accept__sets_the_password_and_signs_in_like_sign_in`, `test_accept__invalid_link_tokens__401_without_a_cookie`, `test_accept__weak_password__422_weak_password`, `test_accept__repeated_failures__429_with_retry_after`, `test_accept_invitation__sets_the_password_and_opens_a_session_with_fresh_tokens`, `test_accept_invitation__repeated_failures_of_one_link__throttled` |
| AC-3.5.6 (API) | `test_forgot__unknown_email__the_same_answer`, `test_forgot__supabase_fails__still_202_and_logged_without_the_email`, `test_forgot__too_many_for_one_email__429_with_retry_after`, `test_reset__sets_the_password_and_signs_in_like_sign_in`, `test_reset__signs_out_the_users_other_sessions`, `test_reset__invalid_link__401_reset_link_refused_without_a_cookie`, `test_reset__authenticator_user__403_code_required_then_200_with_the_code`, `test_reset__ends_every_other_session_of_the_user_here_and_at_supabase` |
| AC-3.5.5 | `test_sign_in__too_many_failures_for_an_email__throttled_even_when_right`, `test_sign_in__too_many_failures_from_an_address__throttled_for_every_email`, `test_sign_in__unknown_email__same_refusal_as_a_wrong_password`, `test_repeated_failures__429_with_retry_after` |

## Verification — 3.5 part 3 (password reset, API) — 2026-10-15

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-3.5.6 (API part) | See the coverage table above | Not yet: the email leg needs Resend connected as Supabase's SMTP (owner step, [operations](operations.md#connecting-resend-for-auth-emails)) and the web pages; until then Supabase's built-in mailer reaches only team addresses | Pass at the API with fakes and recorded Supabase shapes |

**Adversarial checks run (tests):** unknown email vs known email (same status, body and headers), Supabase down or rate limited (still 202), CSRF missing and cross-site, flooding one email from many addresses, address limit shared with failed sign-ins, forged, used and repeated links, another user's refreshed tokens (shared function), wrong authenticator codes and the user's code limit, other sessions of the user revoked and other users' kept, logs grepped for the email.
**Checks run locally:** `ruff format`, `ruff check`, `pytest` (all non-database tests), `python -m tools.openapi --check`. mypy and the `-m db` integration tests run in CI.
**Deployed check:** not yet (after merge, with Resend connected).
**Open issues:** none.
