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

Call budget (placeholder `etsy:app` 5,000 a day): calls per run times runs per day. A reconcile reads the 30-day pages (100 receipts a page); the sweep reads them too, plus the open orders' pages and one count read. The pilot (up to 300 receipts changed in 30 days, up to 100 open orders): 3 + (3 + 1 + 1) = 8 calls a day per shop, so the budget covers about 625 such shops before backfills, the dashboard and webhook re-reads. A third run a day costs one more 30-day read per shop (`daily_etsy_calls(..., runs_per_day=3)`).

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
Etsy Order Intake is not active until the owner approves it (5.11): the worker logs `etsy.webhook_order_not_ingested`. That is expected; reconciliation keeps the store complete.

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

The three automations are built, tested and **`readiness = "draft"`**. The engine refuses their triggers (logged, never retried in a loop), and the API's order sync is the fallback. Activation is the owner's approval (ADR-0012 rule 2); this list is what follows it, in this order:

1. **Approve.** The owner approves Etsy Order Intake, Order Deduplication & Verification and Product Sales Counters.
2. **Ready.** In one reviewed pull request, set `readiness = "ready"` in `registry/automations/etsy-order-intake.toml`, `order-dedup-verification.toml` and `product-sales-counters.toml`. Every section is already `defined` (the loader refuses `ready` otherwise). After it merges, the migrate workflow's `htco registry sync --automations --apply` stores them.
2b. **Count the stored orders.** The history backfill publishes no events, and the events of the draft period were refused, so the counters start empty. For each shop, an owner or admin starts the rebuild once: `POST /v1/automations/product-sales-counters/executions` with an `Idempotency-Key` and the body `{"organization_id": "<org>", "shop_id": "<shop>", "payload": {"rebuild": true}}`. It recounts every stored receipt through the applications ledger, so running it again (or alongside new events) moves nothing. Verify: the execution succeeds with `rebuilt` equal to the shop's stored orders, and a listing's counter equals the units of its orders that count as sales.
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

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
