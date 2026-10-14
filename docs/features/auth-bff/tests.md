# Web sign-in (BFF) — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Platform | Sealing: round trip, fresh nonce, wrong context, tampering, wrong key, malformed blobs, key validation, hidden key | `tests/platform/test_crypto.py` |
| Platform | Supabase Auth client: password and refresh grants, expiry from `expires_at` or `expires_in`, refused credentials, rate limit, service failures, sign-out of this session only, hidden tokens | `tests/platform/test_supabase_auth.py` |
| Application | Sign-in (hashed cookie, lifetimes, refusal without revealing the email, throttling per email and per address, window expiry), resume (fresh, refresh near expiry, refresh refused, idle and absolute expiry, activity extension, unknown and revoked cookies), sign-out | `tests/application/test_web_sessions.py` (fakes in `tests/session_fakes.py`) |
| API | Cookie attributes and no token in the response, cookie authenticates `/v1/me`, wrong password, 429 with `Retry-After`, CSRF on sign-in (login CSRF) and on cookie requests, sign-out, revoked elsewhere, bearer still works, not configured | `tests/api/test_web_auth.py` |
| API | Settings (origins, session key validation) and composition (web sign-in on only with both keys) | `tests/api/test_settings.py`, `tests/api/test_invitation_wiring.py` |
| Platform | Supabase MFA calls: enroll (QR, secret), challenge then verify (`aal2` tokens), wrong or expired codes, factor list, unenroll | `tests/platform/test_supabase_auth.py` |
| Application | Enroll and verify (tokens replaced, step-up time), wrong codes throttled, status, step-up with the verified factor, no factor, the policy's ten-minute window, session list and revoke (own only) | `tests/application/test_mfa.py` |
| API | MFA status, enroll (`no-store`), verify (`aal2`), wrong code, step-up required then allowed then lapsed (injected clock), `mfa-required` at `aal1`, step-up without an authenticator, session list and revoke, session required | `tests/api/test_web_mfa.py` |
| Database (pgTAP) | Session and attempt tables private: RLS on, no privilege for `anon`, `authenticated` or `service_role`, tokens stored as bytes, unique cookie hash, sessions belong to real users | `supabase/tests/database/04_web_sessions.test.sql` |
| Integration (`-m db`) | Session store round trip with tokens sealed at rest, token replacement, touch, revoke, attempt counting per email and per address | `tests/integration/test_session_store.py` |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-3.5.1 | `test_sign_in__sets_only_a_host_only_secure_http_only_strict_cookie`, `test_create_find__round_trips_with_tokens_sealed_at_rest` |
| AC-3.5.2 | `test_sign_in__fails_csrf_checks__403`, `test_unsafe_cookie_request__without_csrf_header__403` |
| AC-3.5.4 | `test_sessions__list_and_revoke_another_device`, `test_revoke_session__another_device__signed_out_there_too`, `test_revoked_elsewhere__next_request_is_401_and_clears_the_cookie`, `test_resume__refresh_refused__ends_the_session`, `test_resume__revoked_session__ended` |
| AC-3.5.3 | `test_owner_without_mfa__mfa_required_for_owner_actions`, `test_enroll_then_verify__session_becomes_aal2` |
| AC-3.5.7 | `test_step_up_required__until_the_code_is_reentered_and_for_ten_minutes`, `test_step_up_window__the_policy_requires_it_and_it_expires_after_ten_minutes` |
| AC-3.5.5 | `test_sign_in__too_many_failures_for_an_email__throttled_even_when_right`, `test_sign_in__too_many_failures_from_an_address__throttled_for_every_email`, `test_sign_in__unknown_email__same_refusal_as_a_wrong_password`, `test_repeated_failures__429_with_retry_after` |
