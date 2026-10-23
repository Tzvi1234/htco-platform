# Notifications — Security

| Threat | Control |
|---|---|
| Buyer data in email | Parameters pass an allowlist of identifier patterns, at the request and again when rendering; the HTML escapes every value |
| Addresses leaking | The address is read at send time through a worker-only function; it is never in the outbox, a log, an error or `email_deliveries`; `EmailMessage` hides it in its repr |
| Spoofing our domain | Sending only from `noreply@office.ht-co.app` once SPF, DKIM and DMARC (`p=none` first) are published; until then nothing is sent |
| A notice blocking sign-in | Security notices are requested after the action and their failures are logged, never raised |
| Reading others' notifications | RLS: own rows only; members may change only `read_at` |
| The Resend key | Secret setting, refused unless it starts with `re_`, hidden in the adapter's repr |
