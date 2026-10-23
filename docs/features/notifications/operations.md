# Notifications — Operations

| Setting (worker) | Default | Meaning |
|---|---|---|
| `HTCO_EMAIL_PROVIDER` | `log` | `log` sends nothing and logs the kind and key; `resend` sends through Resend |
| `HTCO_RESEND_API_KEY` | unset | Required by `resend` |
| `HTCO_EMAIL_REPLY_TO` | `support@ht-co.app` | The `Reply-To` of every email |
| `HTCO_OFFICE_URL` | `https://office.ht-co.app` | Links in emails |

## Turning email on (owner)

1. In Resend, add the domain `office.ht-co.app`; publish its SPF, DKIM and DMARC (`p=none`) records; wait for "verified".
2. Create an API key with sending access only; store it as `HTCO_RESEND_API_KEY` on the worker.
3. Set `HTCO_EMAIL_PROVIDER=resend` and redeploy the worker.

## Runbook

| Symptom | Check | Fix |
|---|---|---|
| No email arrives | `HTCO_EMAIL_PROVIDER`; `email_deliveries` for the user; dead `email.send` outbox messages | Set the provider; fix the address; retry the dead message |
| `failed` deliveries with `resend 403` | The domain is not verified | Finish the DNS step |
