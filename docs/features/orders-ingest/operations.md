# Orders ingest — Operations

## Configuration

| Setting / flag | Default | Effect |
|---|---|---|
| `HTCO_ETSY_WEBHOOKS_ENABLED` | `false` | Turns `POST /webhooks/etsy` on; off answers 404 |
| `HTCO_ETSY_WEBHOOK_SECRET` | unset | The endpoint's signing secret (`whsec_...`), from the GitHub secret `ETSY_WEBHOOK_SECRET`; required when enabled; checked at startup without being repeated |
| `HTCO_ETSY_WEBHOOK_RATE_PER_SECOND` | `50` | Per API instance; above it the answer is 429 with `Retry-After` |
| `HTCO_ORDER_SYNC_IN_API` | `true` | Slice 5.4: the API process runs the order history sync as before (the fallback). `false`: a stale read or the manual sync enqueues `etsy.order_sync` for the worker and returns at once |
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

Until slice 5.9's Prometheus metrics: the logs `etsy.webhook_received`, `etsy.webhook_rejected` (with `reason`), `etsy.webhook_stored_unrouted` and the worker's `etsy.webhook_processed` (with `outcome`). The ingress counts unrouted events by reason (`EtsyWebhookIngress.unrouted`) for 5.9.

### Reconciliation (slice 5.5)

| Signal | Where | Meaning |
|---|---|---|
| `app.sync_cursors.last_succeeded_at` per shop (`reconcile`, `sweep`) | Database (the 5.9 freshness metric reads it) | Older than 30 minutes: reconciliation is not running or failing; `last_error` names the type |
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

## Activating Etsy Order Intake (slice 5.11, the owner)

1. Deploy (the image carries the handler and the registry; `htco registry sync --apply` stores the definition with its `allowed_actions`).
2. The owner approves the activation; the definition moves to `ready` in a reviewed pull request and the registry sync stores it.
3. Set `HTCO_ORDER_SYNC_IN_API` to `"false"`: the API stops syncing; stale reads and the manual sync go to the worker.
3a. Add `orders.reconcile_shops,orders.sweep_shops` to the worker's `HTCO_WORKER_SCHEDULES` (slice 5.5): reconciliation every 10 minutes and nightly.
4. Verify: a webhook or a stale read creates an `etsy-order-intake` execution that succeeds; `app.order_status_events` grows; `orders.ingested.v1` appears in the outbox.

Rollback: `HTCO_ORDER_SYNC_IN_API` back to `"true"` (the API syncs again), and the definition back to `draft` (new triggers refused).

## Rollback

Set `HTCO_ETSY_WEBHOOKS_ENABLED` back to `"false"` (the route answers 404; Etsy retries, then gives up). Stored inbox rows stay and queued jobs still run. Disabling the endpoint in the portal stops deliveries altogether.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
