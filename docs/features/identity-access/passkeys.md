# Identity and access: passkeys as a second factor

| | |
|---|---|
| **Issue** | [#113](https://github.com/Tzvi1234/htco-platform/issues/113) (owner request, 2026-10-25) |
| **Status** | Built behind a flag; **OFF on the hosted project** until the owner decides on Supabase's WebAuthn MFA add-on |
| **Last updated** | 2026-10-26 |

## What and why

Settings › Security offers "Add biometric sign-in (passkey)": Face ID, Touch ID, Windows Hello or an Android fingerprint as the second factor, next to the code from the authenticator app. When the user has a passkey, the sign-in's second step offers it first ("Confirm with Face ID / fingerprint"), with "Use a code instead" as the fallback. Either factor makes the session `aal2`.

A passkey is **phishing-resistant**: the browser signs a challenge only for the site the passkey belongs to, so a look-alike page cannot replay it. A TOTP code typed into a look-alike page can be replayed.

## How it works

Passkeys are Supabase Auth's **WebAuthn MFA factor** (Auth v2.197.0 with Supabase CLI 2.120.0, the versions CI pins). The web app never talks to Auth: the API passes the WebAuthn options and the browser's credential through, as opaque JSON.

| Step | Route | Notes |
|---|---|---|
| Start adding | `POST /v1/auth/mfa/passkeys` `{name}` | Needs an `aal2` session (`403 step-up-required` otherwise). Unfinished passkeys are removed first; a used name gets a number. Answers `{factor_id, challenge_id, ceremony: "create", options}` for `navigator.credentials.create`. |
| Finish adding | `POST /v1/auth/mfa/passkeys/{factor_id}/verify` `{challenge_id, credential}` | Audited `mfa.factor_enrolled`, plus the security notice. |
| Confirm (sign-in or step-up) | `POST /v1/auth/mfa/passkeys/challenge`, then `POST /v1/auth/step-up/passkey` `{factor_id, challenge_id, credential}` | Supabase puts all of the user's passkeys in one challenge, so the browser offers whichever this device has. Refusals count against the same per-user limit as wrong codes (5 per window). |
| Remove a factor | `DELETE /v1/auth/mfa/factors/{factor_id}` | Passkeys and authenticator apps. Needs an `aal2` session. Never the last verified factor, **never the last authenticator app** (`409 last-factor`). Audited `mfa.factor_removed`. |
| Status | `GET /v1/auth/mfa` | Each factor has `kind` (`totp` or `webauthn`); `passkeys` says whether the server offers them. |

Why the last authenticator app stays: it is the recovery when the device with the passkey is lost, and the password reset still asks for its code. The first factor is always the app, because adding a passkey needs an `aal2` session.

## The relying party

The RP ID and origins are **Supabase Auth's server config**, not request parameters. Auth checks the origin and the RP ID hash in every credential, so the client cannot widen them. The API also checks the origin in `clientDataJSON` against `HTCO_WEB_ORIGINS` before Auth sees the credential (`401 passkey-refused`).

| Where | RP ID | Origins | Set in |
|---|---|---|---|
| Local and CI | `localhost` | `http://localhost:5173`, `http://127.0.0.1:5173`, `http://localhost:3000` | `supabase/config.toml` (`[auth.webauthn]`, `[auth.mfa.web_authn]`) |
| Production | `ht-co.app` | `https://dash.ht-co.app` | The hosted project's Auth settings, by the owner. **Not set yet.** |

A passkey is bound to its RP ID, so passkeys made locally never work in production and the other way round. Never `supabase config push` the local `[auth.webauthn]` section to the hosted project.

## Switching it on in production (owner only)

1. Enable the **WebAuthn MFA add-on** on the hosted Supabase project (it has a cost; check the billing page). The CLI refuses to turn on `web_authn` verify without it.
2. In Auth settings, set WebAuthn MFA enroll and verify on, and the RP: display name `HTCO`, RP ID `ht-co.app`, origin `https://dash.ht-co.app`.
3. Set `HTCO_PASSKEYS_ENABLED` to `"true"` in `infra/digitalocean/app.yaml` (a test keeps it `"false"` until then; change the test in the same PR) and deploy.
4. Check: Settings › Security shows the button; add a passkey; sign out and in with it; `audit.audit_log` has `mfa.factor_enrolled` with `after = {"kind": "webauthn"}`.

Rollback: `HTCO_PASSKEYS_ENABLED="false"`. Passkeys already added stay in Auth but are not offered; the authenticator app always works.

## Security

- **No credential material is logged, audited or sent to Sentry.** The options and the credential are `repr`-hidden and pass through untouched; the audit records the factor's ID and kind only. A test checks the logs for the challenge and the signature.
- **Step-up:** adding needs `aal2`, removing needs `aal2` (Auth also refuses to remove a verified factor without it).
- **Origin:** a credential from any other origin is refused, by the API and again by Auth.
- **Throttling:** refused passkeys and wrong codes share one per-user limit.
- **Supabase quirk:** Auth v2.197.0 answers `500` for an assertion that fails validation; the adapter treats a `500` from the verify call as a refusal (it counts against the limit), and any other `500` as Auth being unavailable.

## Tests

- `tests/application/test_passkeys.py`: adding (and several, and the unfinished one), the step-up requirement, the passkey sign-in at `aal1`, refusals and the throttle, the origin check, removal and the last-factor guard, the code step-up choosing the app, switched off.
- `tests/api/test_web_passkeys.py`: the routes, the code fallback, a wrong origin, CSRF, the last factor, the audit, no credential material in the logs.
- `tests/platform/test_supabase_webauthn.py`: the calls to Supabase Auth on a mock transport.
- `tests/infra/test_supabase_passkey_config.py` and `tests/infra/test_app_spec.py`: the local RP is `localhost` only, and production stays off.
