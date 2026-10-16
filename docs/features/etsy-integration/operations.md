# Etsy integration — Operations: read-only rule, probes, disconnect, backups (slices 4.5 and 4.7)

This page is the operations record for the Etsy integration once real shop data exists: the read-only rule, capability probes, disconnecting a shop, and the data protection baseline (backups and the restore rehearsal, AC-4.7.2). Setup of the connect flow and the vault is in [connect flow](connect-flow.md#operations) and [token vault](token-vault.md#operations).

## The read-only rule

The owner's connected shop is live, with real buyers. HTCO never writes to Etsy: no listing, inventory, shop or receipt update, no `createReceiptShipment`, no message, nothing that changes shop data or notifies a buyer. Only GET requests reach Etsy's shop API.

It is enforced in code twice, independently:

1. `EtsyAdapter._request` is the adapter's only way to call Etsy. Any method but GET raises `MarketplaceWriteRefusedError` before the budget or a token is touched, unless the call sets the `allow_write` flag. No code path sets it; a test scans `packages/`, `apps/` and `workers/` and fails if one does.
2. The adapter installs a request hook on its HTTP client. Any non-GET request to `https://openapi.etsy.com/v3/application` is stopped before it leaves the process, even if some code used the client directly. The OAuth token endpoint (`api.etsy.com/v3/public/oauth/token`, a POST that changes no shop data) is outside that path and keeps working.

A refused write logs `etsy.write_refused` (method and path only, never a body). Any such log line is a bug to investigate.

Turning a write on later (for example tracking upload in Phase 6) needs the owner's explicit approval, an ADR update, its own idempotent path through the outbox (never the read retry loop) and a deliberate change to these guards and their tests.

## Capability probes

| | |
|---|---|
| When | Right after a connection completes (best effort: a failing probe never undoes the connection), and when an Owner or Admin presses "Re-check" (`POST /v1/shop-integrations/{shop_id}/capability-checks`, at most once per 30 seconds per shop). Daily runs arrive with the job scheduler in Phase 5. |
| Etsy calls | Three GETs: getShop, one receipt (`limit=1`), one active listing (`limit=1`), all urgent (the owner is waiting). Each uses a token pinned to the probed connection (shop and generation): if the shop is disconnected or connected again meanwhile, possibly by another organization, the probe never borrows that connection's token. |
| Stored | `app.shop_capabilities`: per shop and capability a state, a short detail code and `checked_at` (an older probe finishing late never replaces a newer result); `last_successful_call_at` on the integration when a read succeeded; audit `shop.capabilities_checked` with the states only, all in one transaction. Reconnecting or disconnecting deletes the previous connection's results and moves `connection_generation` on; a probe still running from before, or one a newer probe overtook, stores nothing, sets no success time, writes no audit and answers 409 (its save checks the generation under the row lock), so old results never describe a new connection. |
| Cooldown | A re-check first claims `shop_integrations.capability_check_reserved_at` in one conditional update, so two presses at once (any process) make one probe; the other gets 429 with `Retry-After` rounded up. |
| Never stored or logged | Anything Etsy answered. The receipt check looks only at whether `first_line` and `city` hold text; the values are dropped at once. |

| Capability | Available when | Otherwise |
|---|---|---|
| `granted_scopes` | Every read that needs a scope succeeded (`shops_r`, `transactions_r`, `listings_r`) | `scope_missing` (not granted), `refused_by_etsy` (403), `not_verified` |
| `shop_details` | getShop succeeded | `scope_missing`, `refused_by_etsy` (unavailable); `auth_failed`, `not_found`, `rate_limited`, `etsy_unreachable`, `unexpected_answer`, `etsy_error` (unknown) |
| `receipts_read` | One receipt page was read | As above |
| `buyer_address` | The newest receipt's address fields hold text (`address_fields_present`) | `address_fields_hidden` (Etsy's 2026-07-20 restriction for Israel and others), `no_receipts_yet`, `receipts_unreadable` |
| `listings_read` | One listing page was read | As above |
| `webhooks` | Never, yet | `not_configured` (unavailable) until Phase 5 sets webhooks up; then `configured_not_verified` (unknown) until a signed delivery arrives. Etsy has no API to ask. |
| `shipment_write` | Never | Always `unknown` / `no_safe_probe`: the only way to learn whether `createReceiptShipment` works is to create a shipment, which notifies the buyer. Writes stay disabled; tracking reaches Etsy through AutoVA (HTCO reads it). |

Etsy's `tokenScopes` endpoint is a POST (`POST /v3/application/scopes` in the OpenAPI spec), so the read-only guard would refuse it; the stored scopes are verified by the reads instead.

Runbook:

- **Everything `unknown` / `auth_failed`:** the token was refused. Check the card's token health; reconnect if it shows "needs re-authentication".
- **`rate_limited`:** the daily budget is low or Etsy answered 429; wait and re-check.
- **`buyer_address` unavailable:** Etsy hides addresses for this key and country. Fulfillment uses the partner path (Phase 6), not HTCO.

## Disconnecting a shop

`POST /v1/shop-integrations/{shop_id}/disconnect` (Owner, `aal2`, step-up within 10 minutes, web app with CSRF):

1. The tokens are deleted (envelope columns set to null; the database check refuses a disconnected row with any token part). The tokens are never opened on the way, so a shop whose vault key was lost can still be disconnected.
2. The integration and the shop are marked `disconnected`; `disconnected_at` and `purge_after` (30 days later) are stored, with the audit record `shop.disconnected`, in one transaction.
3. No Etsy call is made for the shop afterwards: the token provider finds no usable integration and refuses before any request (AC-4.7.1), and a re-check answers 409 `shop-not-connected`. The adapter asks for the token again before every attempt, after any throttle or backoff wait, so a call that was retrying when the shop was disconnected stops too; only a request already on the wire at that moment can still complete.
4. Repeating it changes nothing (no second record).
5. Reconnecting clears `disconnected_at` and `purge_after` and marks the shop active again.

**Revocation at Etsy:** Etsy documents no token revocation endpoint (authentication guide and OpenAPI spec, checked 2026-10-16; assumption A-15 resolved). HTCO deletes its copy; the owner can also remove HTCO's access in Etsy's account settings (the exact menu name is UNCONFIRMED). The audit record states `etsy_revocation: not_offered_by_etsy`.

**Purge:** only the date is stored now. The purge job (Phase 5) deletes the shop's synced data after `purge_after`, keeping records under legal retention.

## Backups and the restore rehearsal (AC-4.7.2)

### What the Supabase plan gives us

Checked 2026-10-16 against Supabase's [database backups guide](https://supabase.com/docs/guides/platform/backups):

| Plan | Automatic backups | Point-in-time recovery |
|---|---|---|
| **Free (our project `htco` today, Frankfurt)** | **None.** Supabase backs up Pro, Team and Enterprise projects only and asks Free projects to export their data regularly with `supabase db dump` and keep copies off-site | No |
| Pro ($25/month, the plan in ADR-0005) | Daily, last 7 days (database only, not Storage objects) | Paid add-on (needs at least Small compute) |
| Team / Enterprise | Daily, 14 / up to 30 days | Paid add-on |

So today **no automatic backup exists**. SOFTWARE_DESIGN §9 and ADR-0005 assume Pro. Until the owner moves the project to Pro (a spending decision, his to make), the logical dump below is the only backup. What a loss would cost is small now (the shop's tokens can be re-issued by reconnecting; audit and membership rows would be lost), and grows from Phase 5 when orders are stored.

### Logical backup (Free plan)

Run from a trusted machine with the Supabase CLI (`supabase` ≥ 2) and the database connection string (`HTCO_DATABASE_URL`; the owner holds it). The dump contains sealed tokens (ciphertext only; the vault key is not in the database), audit records and, from Phase 5, buyer data: treat the files as DC2/DC3, encrypt them at rest, never commit them, never paste them into chat.

```sh
mkdir -p backup && cd backup
supabase db dump --db-url "$HTCO_DATABASE_URL" -f roles.sql --role-only
supabase db dump --db-url "$HTCO_DATABASE_URL" -f schema.sql
supabase db dump --db-url "$HTCO_DATABASE_URL" -f data.sql --use-copy --data-only
```

Keep at least the last 7 dumps off-site (for example an encrypted archive in the owner's cloud drive). After the move to Pro this becomes a supplement to the daily backups, not a replacement.

### Restore rehearsal into a scratch local database

Never into the production project, never into a new cloud project without the owner's approval. A local Supabase stack (Docker) is the scratch target:

```sh
# 1. An empty, Supabase-shaped database in a scratch folder (not this repository, so no migrations run).
mkdir scratch-restore && cd scratch-restore && supabase init && supabase start
# 2. Restore the three files in one transaction, triggers off while data loads.
psql --single-transaction --variable ON_ERROR_STOP=1 \
  --file ../backup/roles.sql --file ../backup/schema.sql \
  --command 'SET session_replication_role = replica' \
  --file ../backup/data.sql \
  --dbname "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
```

Checks after the restore (record each result in the table below):

1. Row counts of `app.organizations`, `app.memberships`, `app.shops`, `app.shop_integrations`, `app.shop_capabilities` and `audit.audit_log` equal the production counts taken at dump time.
2. RLS is on for every `app` table (`select relname from pg_class join pg_namespace n on n.oid = relnamespace where nspname = 'app' and relkind = 'r' and not relrowsecurity` is empty).
3. Tenant isolation holds: as one organization's owner (`set role authenticated` with that user's claims), `select count(*) from app.shops` counts only that organization's shops.
4. No plaintext token: `select count(*) from app.shop_integrations where get_byte(access_token_ciphertext, 0) <> 1` is 0.
5. `supabase stop --no-backup` and delete the scratch folder and the decrypted dump files.

### Rehearsal record

| Date | Who | Source | Target | Result | Notes |
|---|---|---|---|---|---|
| — | — | — | — | **Not yet performed** | The procedure above is written and reviewed; it needs Docker and the production connection string, both with the owner. Docker was not available to the agent (2026-10-16), and the agent never holds the production password. The phase exit gate stays open until this row is filled in. |

The phase plan asks for this record before the real shop is connected in production. The owner's shop (LeatherPY) is already connected, so the rehearsal is overdue and is the first owner task of this slice.
