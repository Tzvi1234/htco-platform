# Orders ingest — Operations

## Configuration

| Setting / flag | Default | Effect |
|---|---|---|
| `HTCO_ETSY_WEBHOOKS_ENABLED` | `false` | Turns `POST /webhooks/etsy` on; off answers 404 |
| `HTCO_ETSY_WEBHOOK_SECRET` | unset | The endpoint's signing secret (`whsec_...`), from the GitHub secret `ETSY_WEBHOOK_SECRET`; required when enabled; checked at startup without being repeated |
| `HTCO_ETSY_WEBHOOK_RATE_PER_SECOND` | `50` | Per API instance; above it the answer is 429 with `Retry-After` |
| `HTCO_ORDER_SYNC_IN_API` | `true` | Slice 5.4: the API process runs the order history sync as before (the fallback). `false`: a stale read or the manual sync enqueues `etsy.order_sync` for the worker and returns at once |
| `HTCO_RECONCILIATION_SCHEDULE` | `06:00,18:00` | Slice 5.5, worker: when reconciliation runs, local `HH:MM` comma-separated; the day's last run is the full sweep. Runs only when `orders.reconciliation` is in `HTCO_WORKER_SCHEDULES` |
| `HTCO_RECONCILIATION_TZ` | `Asia/Jerusalem` | The IANA time zone of those times: 06:00 stays 06:00 across a DST change (the image carries `tzdata`) |
| `HTCO_WORKER_ACTION_GATEWAY` | `reads` | Slice 5.4, worker: automations' Etsy reads through the real Action Gateway. `off`: every action refused (`UnavailableGateway`), a switch back without a code change |

The worker needs no other new setting: it processes `etsy.webhook_event` jobs whenever the Etsy app credentials and the vault key are set (as for the probes).

## Registering the webhook in the Etsy developer portal

The owner does this once (webhooks work for personal apps since 2026-02-17):

1. In the Etsy developer portal, open the HTCO app, then **Webhooks** → **Add endpoint**.
2. URL: `https://api.ht-co.app/webhooks/etsy`.
3. Subscribe to the 11 events: `order.paid`, `order.canceled`, `order.shipped`, `order.delivered`, `listing.created`, `listing.updated`, `listing.deleted`, `inventory.created`, `inventory.updated`, `inventory.deleted`, `inventory.out_of_stock`.
4. Copy the endpoint's **signing secret** (it starts with `whsec_`). In GitHub, open the repository's **Settings → Secrets and variables → Actions** and add it as `ETSY_WEBHOOK_SECRET`. Never paste it anywhere else.
5. In a pull request, set `HTCO_ETSY_WEBHOOKS_ENABLED` to `"true"` in `infra/digitalocean/app.yaml`; the next deploy turns the endpoint on.
6. Verify: the portal's test event (or the first real one) shows `204`; the API logs `etsy.webhook_received`; `jobs.inbox` has the row.

To rotate the secret: roll it in the portal, update `ETSY_WEBHOOK_SECRET`, redeploy. During a rotation Etsy may sign with both secrets; any matching `v1` entry passes.

## Metrics and alerts

Admin › Health (slice 5.9) shows the queue, the automations' metrics, the shops' sync freshness and the webhook intake. Logs: the logs `etsy.webhook_received`, `etsy.webhook_rejected` (with `reason`), `etsy.webhook_stored_unrouted` and the worker's `etsy.webhook_processed` (with `outcome`). The ingress counts unrouted events by reason (`EtsyWebhookIngress.unrouted`) for 5.9.

### Reconciliation (slice 5.5)

Call budget (placeholder `etsy:app` 5,000 a day): calls per run times runs per day. A reconcile reads the 30-day pages (100 receipts a page); the sweep reads them too, plus the open orders' pages and one count read. The pilot (up to 300 receipts changed in 30 days, up to 100 open orders): 3 + (3 + 1 + 1) = 8 calls a day per shop, so the budget covers about 625 such shops before backfills, the dashboard and webhook re-reads. Order Deduplication & Verification (5.11) adds one `etsy.receipts.read` per ingested or updated order from a webhook or a sync, and none for reconciliation's own events (it has just compared those receipts). A third run a day costs one more 30-day read per shop (`daily_etsy_calls(..., runs_per_day=3)`).

| Signal | Where | Meaning |
|---|---|---|
| `app.sync_cursors.last_succeeded_at` per shop (`reconcile`, `sweep`) | Database (the 5.9 freshness metric reads it) | Older than 13 hours (`reconcile`) or 25 hours (`sweep`): reconciliation is not running or failing; `last_error` names the type |
| New rows in `app.sync_drifts` | Database, `orders.drift_detected.v1` | `missing`: a webhook was lost or arrived late; `status`, `items`, `shipments`: the store differed from Etsy and was corrected |
| The sweep's `etsy_receipts` and `orders_stored` (execution output) | `app.automation_executions` | A gap means the backfill is incomplete; a manual sync resumes it |

## Runbook

### Etsy shows failed deliveries
1. **Check:** the API logs `etsy.webhook_rejected` and its `reason`.
2. **Likely cause:** `bad_signature` (wrong or rotated secret), `stale_timestamp` (clock skew), `rate_limited` (a burst).
3. **Fix:** update the secret; Etsy retries for about 30 hours, and the portal can replay failed messages.
4. **Verify:** new deliveries answer 204. Reconciliation (5.5) fills any gap meanwhile.

### Orders arrive by webhook but are not stored
Etsy Order Intake is active since #157 (5.11). If it is back to `draft` (a rollback), the worker logs `etsy.webhook_order_not_ingested`; that is expected, and reconciliation keeps the store complete.

### Etsy Order Intake fails or dead-letters (slice 5.11)
1. **Check:** Admin › Health: the Automations row (`etsy-order-intake`: success rate, retries, dead letters) and the dead letters' last error. Open the execution for its chain (`GET /v1/executions/{id}`).
2. **Likely cause:** `MarketplaceRateLimitedError` or a deferral (the Etsy budget or a 429), `IntakeActionError` (the gateway refused or failed the read), `ShopBusyError` (another sync of the shop held its lock: retried, never lost), a database error (shown as its type and SQLSTATE).
3. **Fix:** fix the cause, then **Retry the execution** in its sheet (audited; the same execution, a new attempt). Ingest is idempotent, so a retry never doubles an order. A webhook's event stays in `jobs.inbox` meanwhile, and reconciliation re-reads every receipt changed in 30 days.
4. **Verify:** the execution reaches `succeeded`; the order is in Orders with its timeline.

### Order Deduplication & Verification records a drift (slice 5.11)
1. **Check:** `app.sync_drifts` with `source = 'verification'` and its kind: `missing` (no stored order for a receipt Etsy has), `totals` (the stored grand total differs; the row names the field, never the amount), `items` (item IDs or quantities differ, both sides recorded). Each drift has an `orders.drift_detected.v1` event.
2. **Likely cause:** an ingest that failed half-way for an older version, a manual change at Etsy, or a bug in the receipt mapping.
3. **Fix:** a drift is a finding, never a change. Re-sync the order from the Orders screen (**Re-sync selected**) or let reconciliation re-read it; a new version is verified again. From Phase 6 a drift opens an incident.
4. **Verify:** the next verification of the order's version records nothing new (a drift is unique per receipt, kind and Etsy update time).

### Product Sales Counters look wrong (slice 5.11)
1. **Check:** `app.product_sales_counters` for the listing, and `app.product_sales_applications` for the receipts that count it (`counted`: listing → units; empty for unpaid, cancelled or refunded orders).
2. **Likely cause:** the automation is not active yet (no execution), it dead-lettered (Admin › Health), or an order's stored state is behind Etsy.
3. **Fix:** retry the dead-lettered execution, or run the **rebuild** for the shop (as in [activation step 2b](#activation-checklist-slice-511-for-the-owner)). Each run moves the counters by the difference only (through `app.product_sales_applications`), so re-running is always safe.
4. **Verify:** the counters equal the units of the listing's orders that count as sales.

## Activation checklist (slice 5.11, for the owner)

The owner approved the activation (ADR-0012 rule 2), and #157 took steps 2 to 4: the three automations are **`readiness = "ready"`**, the API creates executions for the worker (`HTCO_AUTOMATIONS_ENABLED`), and the worker owns the order sync and reconciliation. Before that they were `draft`: the engine refused their triggers (logged, never retried in a loop) and the API's order sync was the fallback. The list, in order:

1. **Approve.** The owner approves Etsy Order Intake, Order Deduplication & Verification and Product Sales Counters.
2. **Ready.** In one reviewed pull request, set `readiness = "ready"` in `registry/automations/etsy-order-intake.toml`, `order-dedup-verification.toml` and `product-sales-counters.toml`. Every section is already `defined` (the loader refuses `ready` otherwise). After it merges, the migrate workflow's `htco registry sync --automations --apply` stores them.
2b. **Count the stored orders.** The history backfill publishes no events, and the events of the draft period were refused, so the counters start empty. For each shop, an owner or admin starts the rebuild once: `POST /v1/automations/product-sales-counters/executions` with an `Idempotency-Key` and the body `{"organization_id": "<org>", "shop_id": "<shop>", "payload": {"rebuild": true}}`. It recounts every stored receipt through the applications ledger, so running it again (or alongside new events) moves nothing. The rebuild runs the whole shop in one execution (`timeout_seconds = 120`): enough for the pilot shop, and a timeout is safe because the retry resumes through the ledger. **Later task:** for large shops, checkpoint the last receipt ID (`after`) in the execution, or start one execution per page. Verify: the execution succeeds with `rebuilt` equal to the shop's stored orders, and a listing's counter equals the units of its orders that count as sales.
3. **Worker sync.** Set `HTCO_ORDER_SYNC_IN_API` to `"false"` in `infra/digitalocean/app.yaml` (the API service). The API stops syncing; stale reads, the manual sync and "Re-sync selected" go to the worker as `etsy.order_sync` jobs.
4. **Reconciliation.** Add `orders.reconciliation` to the worker's `HTCO_WORKER_SCHEDULES`. Reconciliation then runs at the times of `HTCO_RECONCILIATION_SCHEDULE` (06:00 and 18:00 Asia/Jerusalem by default; the 18:00 run is the full sweep).
5. **Webhooks.** Register the endpoint and its secret in the Etsy developer portal ([above](#registering-the-webhook-in-the-etsy-developer-portal)), then set `HTCO_ETSY_WEBHOOKS_ENABLED` to `"true"`.
6. **Verify**, within the first hour:
   - a webhook or a stale read creates an `etsy-order-intake` execution that succeeds;
   - its `orders.ingested.v1` starts one `order-dedup-verification` and one `product-sales-counters` execution;
   - Admin › Health shows the three automations with their success rates;
   - the Orders screen says "Synced from Etsy".

Rollback, in reverse order and each step on its own:
- `HTCO_ETSY_WEBHOOKS_ENABLED` back to `"false"`;
- remove `orders.reconciliation` from the schedules;
- `HTCO_ORDER_SYNC_IN_API` back to `"true"` (the API syncs again);
- the definitions back to `"draft"` (new triggers are refused and running executions finish).

## Rollback

Set `HTCO_ETSY_WEBHOOKS_ENABLED` back to `"false"` (the route answers 404; Etsy retries, then gives up). Stored inbox rows stay and queued jobs still run. Disabling the endpoint in the portal stops deliveries altogether.

## Drills (slice 5.8)

### Kill-worker drill (automated, every pull request)

What it proves: a worker killed in the middle of a job leaves the job to its lease, and the next worker completes it exactly once (AC-5.1.1); an automation execution killed while `running` ends in one `succeeded` execution and one business effect (AC-5.10.5); a crash between the business commit and the delivery delivers once (AC-5.2.2).

How it runs: CI's `database` job starts real worker processes (`tests/integration/worker_harness.py`, the production loop and signal handling) against Postgres and sends them `SIGKILL` mid-job (`tests/integration/test_worker_process.py`):

| Test | What is killed | Expected and checked |
|---|---|---|
| `test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart` | A worker holding a job's lease | After the lease expires, a second worker completes the job; its effect is recorded once |
| `test_worker_killed_during_a_running_execution__one_success_and_one_effect` | A worker running an automation execution | One `succeeded` execution, attempts 2, one effect |
| `test_crash_after_the_business_commit__the_effect_is_delivered_exactly_once` | A worker after the commit, before the outbox delivery | One delivery after restart |
| `test_sigterm__the_current_job_finishes_before_the_worker_exits` and `…a_job_beyond_the_grace_period__is_left_to_its_lease` | A worker stopped with `SIGTERM` | The job finishes inside the grace period, or is left to its lease |

In the deployed environment the same drill is: from DigitalOcean, restart the `agent-worker` component while Admin › Health shows a running job. The worker disappears from Workers as "Stopped" after two minutes, the new instance appears as "Running", and the job ends `succeeded` (or `retry_scheduled`, then `succeeded`). The owner records the run in the table below.

### Restore drill (documented; the owner runs it)

The Supabase Free plan has no backups and no point-in-time recovery, so the drill is a manual `pg_dump` of the production data, restored into a local scratch database built from the same migrations. The dump holds buyer names and addresses (`app.etsy_order_buyers`), so it is personal data: keep it on the owner's own machine, never commit, upload or share it, and delete it when the drill ends.

What you need: the Supabase CLI and Docker on the owner's machine, the production database password (`SUPABASE_DB_PASSWORD`), and the session pooler connection string (Supabase dashboard → Connect → Session pooler, port 5432).

1. **Dump** the HTCO schemas' data from production (read-only; it takes a consistent snapshot):

   ```sh
   export PGPASSWORD='<production database password>'
   pg_dump "postgresql://postgres.<project-ref>@<pooler-host>:5432/postgres?sslmode=require" \
     --format=custom --data-only --no-owner --no-privileges \
     --schema=app --schema=jobs --schema=audit --schema=private \
     --file=htco-restore-drill.dump
   ```

   The `auth` schema stays out: users are restored by inviting them again, and the restore below runs as the database owner. Note the start and end time.

2. **Build the scratch database** from the repository at the commit production runs: `supabase start`, then `supabase db reset`, which applies every migration and the seed (`supabase/seed.sql`). Empty the seed's rows from the HTCO schemas before restoring, so counts compare one to one.

3. **Restore** the data:

   ```sh
   pg_restore --data-only --no-owner --disable-triggers \
     --dbname=postgresql://postgres:postgres@127.0.0.1:54322/postgres htco-restore-drill.dump
   ```

   `--disable-triggers` lets the restore load the append-only and state-machine tables (audit, execution and order status events) in their recorded order.

4. **Verify the row counts.** In production and in the scratch database, run the same query and compare:

   ```sql
   select 'etsy_orders' as t, count(*) from app.etsy_orders
   union all select 'etsy_order_items', count(*) from app.etsy_order_items
   union all select 'etsy_order_buyers', count(*) from app.etsy_order_buyers
   union all select 'order_status_events', count(*) from app.order_status_events
   union all select 'financial_entries', count(*) from app.financial_entries
   union all select 'automation_executions', count(*) from app.automation_executions
   union all select 'action_executions', count(*) from app.action_executions
   union all select 'jobs', count(*) from jobs.jobs
   union all select 'audit_log', count(*) from audit.audit_log
   order by 1;
   ```

   Every count must match; a smaller count names the table to look at.

5. **Verify tenant isolation, buyer data included.** In the scratch database, sign in as a member of each organization through SQL and check that one organization never sees another's rows, and that a role without buyer data sees no buyer row:

   ```sql
   begin;
   select set_config('request.jwt.claims',
     '{"sub":"<a member user id>","role":"authenticated","aal":"aal2"}', true);
   set local role authenticated;
   select count(*) from app.etsy_orders;          -- only that member's shops
   select count(*) from app.etsy_order_buyers;    -- 0 for Finance, Developer and Viewer
   rollback;
   ```

   The users come from `app.memberships`; insert matching `auth.users` rows (id and email) in the scratch database first. pgTAP files `08`, `17` and `21` encode the same rules and must pass there too: `supabase test db`.

6. **Clean up:** `supabase stop --no-backup`, delete `htco-restore-drill.dump` and empty the trash, and unset `PGPASSWORD`.

7. **Record** the date, how long each step took, the outcome and any follow-ups in the table below.

Follow-up for when the cost is approved: Supabase Pro adds daily backups and point-in-time recovery (ADR-0005). The drill then restores a backup into a new project instead of a `pg_dump`.

### Drill log

| Date | Drill | Duration | Outcome | Follow-ups |
|---|---|---|---|---|
| 2026-10-23 | Kill-worker drill, automated (CI `database` job, the five tests above) | The `database` job takes about 3 minutes, process tests included | Passed on every pull request of slices 5.0 to 5.11 | Run it once in the deployed environment once the worker carries the order automations (the owner) |
| — | Restore drill (steps above) | — | Not run yet: it needs the production password and the owner's machine | The owner runs it and records it here |
