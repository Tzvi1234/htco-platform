# Web sign-in (BFF) — Operations

Settings are listed in the [overview](overview.md#configuration) and in the [platform operations](../platform-skeleton/operations.md) environment table.

## Connecting Resend for Auth emails

Supabase Auth sends the password reset email itself. The API does not depend on the provider: until custom SMTP is set, Supabase's built-in mailer delivers only to the project team's addresses, with a low hourly limit. To deliver to every user, the owner connects Resend in the dashboard (an owner step, not code):

1. In Resend, verify a sending domain under `ht-co.app` (SPF and DKIM records; for example `office.ht-co.app`, which Phase 6 uses for transactional email) and create an API key limited to sending.
2. In Supabase → Authentication → Emails → SMTP Settings, enable custom SMTP:
   - Host `smtp.resend.com`, port `465`.
   - Username `resend`, password = the Resend API key (a secret: typed into the dashboard only, never committed or pasted into chat).
   - Sender email on the verified domain (for example `noreply@office.ht-co.app`) and sender name `HTCO`.
3. In Supabase → Authentication → Rate Limits, review the email limit (it rises once custom SMTP is on).
4. In Supabase → Authentication → URL Configuration, add `https://dash.ht-co.app/reset-password` to the redirect URLs (next to `https://dash.ht-co.app/accept-invite`). Without it, Supabase sends the link to the Site URL instead.
5. Optionally edit the "Reset password" email template (Hebrew and English text); keep `{{ .ConfirmationURL }}` as the link.
6. Check: ask for a reset at https://dash.ht-co.app/forgot-password with an account's email; the email arrives from the verified sender; the link opens the reset page; after the reset, other browsers of that user are signed out.

## Runbook

| Symptom | Check | Fix |
|---|---|---|
| `POST /v1/auth/password/forgot` answers 503 | `HTCO_PASSWORD_RESET_REDIRECT_URL` and the web sign-in settings in the app spec | Set the URL; redeploy |
| Users get 202 but no email | Logs for `auth.password_reset_email_failed`; Supabase → Logs → Auth for `/recover` errors | Supabase rate limit: wait or raise it; SMTP: check the Resend key and domain; before Resend is connected, only team addresses receive mail |
| The link opens the Site URL, not the reset page | Supabase redirect URLs | Add `https://dash.ht-co.app/reset-password` |
| The reset page says the link expired | The link was used or is older than Supabase's email OTP expiry | Ask for a new link |
| Many 429 `too-many-attempts` from one office address | 20 failures or requests per address in 15 minutes are shared with sign-in | Wait 15 minutes; check for abuse in `private.sign_in_attempts` (hashed) |
