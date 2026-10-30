# Things to remember / דברים לזכור

| | |
|---|---|
| **Purpose** | Owner steps and decisions that wait for a later moment, each with its context and what it blocks. Tick an item in the pull request that closes it; never delete one. |
| **Started** | 2026-10-28, from the owner's decisions of that day and the plan audit |
| **Decisions** | [ASSUMPTIONS.md](../product/ASSUMPTIONS.md) §1 · [PLAN.md](../plan/PLAN.md) |

## Waiting for a business event

- [ ] **Supabase Pro backups and the restore drill: activate when the product is first sold.** The owner's decision of 2026-10-28 ([D-28](../product/ASSUMPTIONS.md)): no real customer yet, so the project stays on the Free plan, which has no automatic backups. At the first sale: move to Pro (daily backups or PITR, D-09), run the restore drill from the runbook, and record it. *Blocks:* AC-4.7.2 and 4.7's "daily backups confirmed" ([Phase 4](../plan/phases/phase-04-etsy-connection.md)), AC-5.8.1's restore drill ([Phase 5](../plan/phases/phase-05-orders-ingest.md)), and both phases' exit gates.

## With the owner, at the next opportunity

- [ ] **Connect Resend and the DNS for `office.ht-co.app`, together with the owner** ([D-29](../product/ASSUMPTIONS.md)). The code is built and OFF: SPF, DKIM and DMARC records, then Resend as Supabase Auth's custom SMTP and as the notification sender ([auth-bff operations](../features/auth-bff/operations.md#connecting-resend-for-auth-emails)). *Blocks:* AC-3.5.6, the password reset end to end ([Phase 3](../plan/phases/phase-03-design-system-web-shell.md)), and 6.4's email sending ([Phase 6](../plan/phases/phase-06-fulfillment-incidents.md)).
- [ ] **The worker's dedicated database login role.** The owner sets its password in Supabase and adds the new secret named in the architecture-fixes pull request. *Blocks:* deploying that pull request; until then the worker keeps its current role.
- [ ] **`HTCO_SENTRY_DSN` for the worker, and a Sentry volume alert per `automation_id`.** The owner adds the DSN secret and, in Sentry, a new-issue and volume alert rule keyed on the `automation_id` tag. *Blocks:* AC-6.7.4 reaching the owner in production (6.7).
- [ ] **Supabase Auth: the reset email and the redirect allow-list for the link callback** (Codex security review, owner gate v4). Before the security-fix pull request deploys: in Supabase Auth, URL Configuration, add `https://dash.ht-co.app/api/v1/auth/links/callback` to the redirect allow-list; in Email Templates, Reset Password, change the link to `{{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=recovery` (and the Invite template the same way with `type=invite`, for when Supabase sends invitations itself). The Site URL stays as it is. Invitation links are built by the API and need no template. *Blocks:* emailed password reset links (they answer `?link=invalid` until then) ([auth-bff](../features/auth-bff/overview.md)).
- [ ] **Etsy app: re-register the redirect URI** (Codex security review, owner gate v4). Before the security-fix pull request deploys: in the Etsy developer portal, change the app's callback URL to `https://dash.ht-co.app/api/v1/integrations/etsy/callback` (it was `https://dash.ht-co.app/integrations/etsy/callback`). *Blocks:* connecting and reconnecting a shop; Etsy refuses a redirect URI that does not match exactly ([connect flow](../features/etsy-integration/connect-flow.md)).

## When the owner decides

- [ ] **Activate the Phase 6 automations** (Tracking Synchronization and Incident Detection & Escalation, readiness `draft` to `ready`). *Blocks:* everything of Phase 6 in production: relay items, breaches, ship-by and review alerts, drift incidents (6.7).

## When a phase or slice starts

- [ ] **AC-6.7.5 against rule P1-4: revisit when 7.1 lands.** The owner's decision of 2026-10-28 ([D-26](../product/ASSUMPTIONS.md)): AC-6.7.5 waits for the 7.1 task model (#181), an exception to P1-4 for now. *Blocks:* 6.7's AC-6.7.5 and its web step AC-7.19.4.
- [ ] **The OpenRouter and Jev decision, at the start of 9.1.** The owner researches OpenRouter as the routing layer across open-source models, possibly with Jev, first; ask him when 9.1 starts ([Phase 9](../plan/phases/phase-09-router-advisor.md)). *Blocks:* 9.1's provider adapters and 9.2's catalog rows.

## Dated

- [ ] **The OSV exception for `deepmerge` expires on 2026-11-27.** Before then, upgrade or replace the dependency, or renew the exception with a reason. *Blocks:* the vulnerability scan stays green only until that date.
