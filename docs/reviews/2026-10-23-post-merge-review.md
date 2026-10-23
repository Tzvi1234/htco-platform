# Post-merge review, 2026-10-23

The Codex review bot's quota ran out, so six pull requests merged without a final review:

- #57: order history store, backfill and refunds.
- #61: app-key fingerprint, `needs_reauth`, `DELETE /v1/shops/{id}`.
- #53: job queue and agent-worker.
- #62: inbox, outbox and `Idempotency-Key`.
- #58: durable automation engine.
- #54: agent-worker deploy.

This review read each diff and the code on `main` at `3b8d22e`, which includes slice 5.12. Line numbers refer to that commit.

Severity:

- **P0:** live harm now.
- **P1:** a security, privacy or data-loss defect that ships with the next deploy.
- **P2:** a correctness defect with a concrete failure.
- **P3:** a risk or gap worth tracking.

The fix branch `fix/post-merge-review-2026-10-23` fixes every P1 and P2 finding with a failing test first.

## Findings

### P1-1: the worker's purge keeps a disconnected shop's buyer data for ever (#53, #57)

- **Where:**
  - `packages/persistence/src/htco_persistence/maintenance.py:21` and `:63-77`.
  - `supabase/migrations/20261022120000_etsy_order_history.sql:202-221`.
- **What it does:** the daily `shops.purge` job deletes only `app.shop_capabilities`, then sets `purge_after = null`.
  - The order-history purge, `private.purge_disconnected_etsy_orders`, selects shops by `purge_after <= now`, so it can never match that shop again.
  - Nothing calls that function anyway: `PostgresOrderStore.purge_disconnected` is only called from tests.
- **Why it matters:** #54 enables `maintenance.purge_disconnected_shops` in production. After the first purge of a shop disconnected 30 days earlier, its orders stay, and with them its buyers' names, addresses and tracking numbers. They stay with no remaining path to delete them. This breaks DATA_MODEL §6 and FR-SHOP-04.
- **Scenario:**
  1. The Owner disconnects shop S on day 0. `purge_after` is day 30.
  2. On day 31 the worker purges S. The capabilities are deleted and `purge_after` becomes null.
  3. The rows in `app.etsy_order_buyers` for S stay indefinitely.
- **Fix (done):**
  - The purge also deletes `app.etsy_orders` and `app.etsy_order_sync_state`. Items, refunds and buyer rows cascade, and it all happens in the same transaction that checks the dates and clears `purge_after`.
  - **Migration `20261023190000_worker_order_purge.sql`** grants `htco_worker` `delete` on those two tables, plus `select` on their `organization_id` and `shop_id` columns only.
  - It adds organization-scoped RLS policies for `select` and `delete`.
  - The worker still cannot read the buyer, item or refund tables.
- **Tests:**
  - `tests/integration/test_job_queue.py::test_purge__deletes_the_shops_stored_order_history_with_its_buyer_rows`.
  - `tests/integration/test_job_queue.py::test_worker_role__never_reads_buyer_rows_or_another_organizations_orders`.
  - `supabase/tests/database/14_worker_order_purge.test.sql`.
- **Before deploying:** look in `audit.audit_log` for `shop.purged` records. Any shop purged before this fix still has its orders. Delete them with `delete from app.etsy_orders where shop_id = …` and the same for `app.etsy_order_sync_state`, as the login role.

### P2-1: two runs of one automation execution can overlap (#58)

- **Where:** `packages/application/src/htco_application/automation/durable.py:172-187` (`execution_job`) and `:479-490` (`_recover`).
- **What it does:** execution jobs carry no `lock_key`, and `execute` treats an execution found `RUNNING` as the run of a dead worker.
- **Scenario:**
  1. Worker A runs job 1. The handler fails, and `_fail` stores `RETRY_SCHEDULED` with job 2, due in 5 seconds.
  2. Worker A dies before it completes job 1.
  3. Job 1's lease expires and it is claimed again. The execution goes `RUNNING` and the handler starts.
  4. Job 2 comes due while that handler is still running. It sees `RUNNING`, "recovers" it and runs the handler a second time, at the same moment.
  5. The attempts check later refuses the first run's result, but its side effects already happened twice.
- **Fix (done):** every job of an execution has `lock_key = automation_execution:{id}`. The queue's partial unique index then never runs two of them at once (AC-5.1.2). `RUNNING` is found only when no other job of that execution holds the lock, which means its worker really died.
- **Test:** `tests/application/test_durable_engine.py::test_execution_jobs__share_one_lock_per_execution__so_two_runs_never_overlap`.

### P2-2: an app-key fingerprint that changes with whitespace marks every healthy shop `needs_reauth` (#61)

- **Where:**
  - `packages/integrations/src/htco_integrations/etsy.py:130-136`.
  - `packages/persistence/src/htco_persistence/shop_integrations.py:82-117` (`retire_other_app_keys`, run at API startup).
- **What it does:** the fingerprint hashes the raw setting.
- **Scenario:**
  1. The same keystring is re-entered in App Platform with a trailing newline or space.
  2. Etsy calls keep working, because HTTP drops the whitespace around a header value.
  3. The fingerprint changes, though, and the startup sweep marks every connected shop `needs_reauth`.
  4. The status is stored and audited, and nothing reverts it. Every Owner must reconnect every shop.
- **Fix (done):** `app_key_fingerprint` hashes `keystring.strip()`. For a key without whitespace the hash is unchanged, so stored fingerprints still match.
- **Test:** `tests/integrations/test_etsy_adapter.py::test_app_key_fingerprint__the_same_key_pasted_with_surrounding_whitespace__the_same_app`.

### P2-3: late refunds and cancellations never reach the stored order history (#57)

- **Where:** `packages/application/src/htco_application/order_history.py:291-325`.
- **What it does:** the incremental sync reads receipts *created* in the last 30 days.
- **Scenario:**
  1. An order placed 45 days ago is backfilled as a sale.
  2. Today it is refunded in full.
  3. The stored copy never changes. Orders shows it paid, and Statistics counts it as a sale with no refund reason.
  4. The live read falls back to the store once the backfill is complete, so the error is permanent.
- **Fix (done):** the incremental sync reads receipts *changed* in the last 30 days, using Etsy's `min_last_modified` on `getShopReceipts` (still a GET through the guarded adapter). That set includes every order created in that window.
  - Paging stays newest-first by creation date.
  - A receipt that changes during the read only moves into the set, which repeats a row and never skips one.
- **Tests:**
  - `tests/application/test_order_history.py::test_incremental__a_late_refund_of_an_order_older_than_30_days__reaches_the_store`.
  - The adapter test `test_backfill_through_the_adapter__gets_only_and_every_page`, which now checks the query parameter.

### P3: recorded, not changed here

1. **The app-key sweep cannot be undone** (#61, `shop_integrations.py:82`). A deploy with a wrong keystring, for example staging's, marks every live shop `needs_reauth` for good, even after the setting is fixed.
   - The read path already treats a mismatch as `needs_reauth` without storing it. Consider not storing it at all, or storing a reason so that a later matching key reverts only the rows the sweep changed.
   - Until then: verify the keystring before any deploy that changes it.
2. **A stuck `Idempotency-Key`** (#62, `htco_persistence/idempotency.py:36-70`). If the process is killed hard mid-request (OOM or SIGKILL), its `in_progress` row stays for 24 hours, and every retry with that key answers 409 `idempotency-in-progress`.
   - Fix: also take over an `in_progress` row older than a few minutes.
3. **One bad outbox payload blocks all lease releases** (#62, `20261023130000_inbox_outbox.sql`, `private.sync_outbox_with_job`). The trigger casts `payload->>'outbox_id'` to uuid, so an `outbox.deliver` job with a malformed payload makes the update fail.
   - That includes `release_expired`, which updates every expired job in one statement.
   - Only code enqueues these jobs today. A guarded cast, or `null`, would isolate one bad row.
4. **A worker that lost its lease keeps running its job** (#53, `htco_worker/runner.py`). It is no longer `running`, so the job's `lock_key` is free while the handler still runs.
   - This is at-least-once by design, but handlers must stay idempotent. P2-1 narrows it for automations.
5. **`note_from_issuer` is readable by every shop member at the database level** (#57). RLS on `app.etsy_order_refunds` follows shop access, while the API returns the note only to buyer-data roles.
   - The seller's note may quote the buyer. Consider moving it to `etsy_order_buyers`, or nulling it for other roles in a view.
6. **The old Etsy shop's orders stay** (#57, `etsy_orders.py` `_ENSURE_STATE`). When another Etsy shop is connected behind the same HTCO shop, the previous Etsy shop's orders and buyer rows are kept while the integration stays connected, so no purge applies.
   - Delete rows of other `etsy_shop_id`s when the backfill restarts.
7. **Unique buyers use stored names for every role** (#57, `shop_dashboard.py:626`). Statistics reads buyer names for any role that sees the overview, but answers only counts.
   - Acceptable, and documented in the code. Keep names out of any future per-row output.
8. **Worker identity** (#53, `htco_worker/settings.py:26`). The default `hostname-pid` relies on App Platform giving each instance a unique hostname. Two processes with the same ID could complete each other's jobs.
9. **Requests from the client review** (htco-clients PR #42):
   - Shipping needs every paid, unshipped order regardless of age. Provide a `state=paid` read with no period limit.
   - Overview and Statistics take a fixed `utc_offset_minutes`, so days and months are an hour off across a DST change. Accept an IANA `time_zone` instead.

## Checked and sound

- **GET-only on Etsy:**
  - Every Etsy call in the API and the worker goes through `EtsyAdapter._request`, which refuses any method but GET towards the shop API.
  - `DELETE /v1/shops/{id}` and Disconnect make no Etsy call.
  - The outbox has no Etsy destination, and the automation engine has no deployed handler. The gateway from 5.12 is outside this review.
- **Buyer data RLS:** `private.user_buyer_data_shop_ids` gives Owner and Admin every shop of their organizations, and Operations and Support their granted shops. The API applies the same rule (`_sees_buyer_contact`) before it reads the buyer table.
- **PII in logs and audit:**
  - Sync failures log and store the exception type only, and `last_error` is constrained to a type name.
  - Removal and purge audits carry counts and the shop's name only.
  - `OrderRefund.note_from_issuer` and the buyer fields are `repr=False`.
- **Destructive routes:** Disconnect and Remove go through `resume`, which runs `check_csrf` on unsafe methods, then `connect_shop` (Owner, `aal2`, a step-up in the last ten minutes) for the shop's own organization. A repeated Remove answers 404.
- **Worker tenant isolation:**
  - Every worker connection sets `htco.organization_id` from the job when it is taken from the pool.
  - Tenant tables compare rows with it, and a job without an organization sees no tenant row.
  - The fan-out lists only IDs, through security-definer functions.
- **Queue:**
  - The claim uses `FOR UPDATE SKIP LOCKED` with the partial unique index on running lock keys.
  - Complete and fail check `leased_by`.
  - An expired lease counts as an attempt, so a job that crashes every time still dead-letters.
- **Upsert ordering:** `where o.etsy_updated_at <= excluded.etsy_updated_at` keeps an older copy from overwriting a newer one, and each page commits with its checkpoint.
- **Migrations:** every migration in these PRs creates new objects or adds a nullable column with a check constraint, so there is no rewrite or backfill of a live table. The index `shop_integrations_connected_app_key_idx` is built without `concurrently` on a table of a few rows.
- **Deploy (#54):** secrets are `type: SECRET`, the worker has one fixed instance, and the budget is $20.
