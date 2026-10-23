# Orders ingest — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Application | Secret parsing; verification (valid, rotation, every refusal, window edges, header case); parsing of the 11 recorded payloads; the ingress (routed jobs, replay, two IDs for one change, unknown type or shop, no receipt); the rate window; the worker's job (intake trigger, refusal, listing and stock re-reads, out-of-stock confirmation, replays, disconnected shop, malformed jobs) | `tests/application/test_etsy_webhooks.py` |
| API | Off by default; 204 stored; replay; 401 cases logged without the body; 400; 413 (declared and streamed); 429 then accepted; 20 concurrent at p95 under 1 s; settings and wiring | `tests/api/test_webhook_routes.py`, `tests/api/test_webhook_wiring.py` |
| Adapter | `get_listing` and `get_listing_inventory`, each one GET against a strict mock | `tests/integrations/test_etsy_webhook_reads.py` |
| Worker | The job through the real adapter, GETs only | `tests/worker/test_webhook_jobs.py` |
| Integration (Postgres) | Routing to the connected shop, one job per change; unrouted rows; processed marks and shop lookup as `htco_worker`; intake triggered once from the worker's role | `tests/integration/test_etsy_webhooks.py` |
| Database (pgTAP) | The worker inserts executions of its organization only | `supabase/tests/database/15_etsy_webhook_intake.test.sql` |

Recorded payloads: `tests/fixtures/etsy/webhooks/events.json` (Etsy's documented shape, invented IDs).

### Slice 5.4

| Level | What is covered | Location |
|---|---|---|
| Domain | Every allowed and every illegal transition; observed states; ingest paths; paths are legal (property); counts exclude cancelled, refunded and unpaid (property); exact money, floats refused | `tests/domain/test_orders.py` |
| Application | `plan_ingest`: new order, replays, older copies, newer copies, cancel and refund events, behind, backfill without events, delivered; Decimal entries; no buyer data in the hash or events; JSON round trip (property) | `tests/application/test_order_ingest.py` |
| Application | The handler through the real gateway and API channel: receipt re-read with its buyer row, ten replays, delivered, disconnected, denied, deferred, history run (backfill then 30 days), lock held; the channel's automation-only form; the API's queued requests; the worker's sync trigger | `tests/application/test_order_intake.py`, `tests/application/test_order_sync_requests.py` |
| Adapter | `get_order_receipt` one GET on a strict mock; fake contact values kept nowhere | `tests/integrations/test_etsy_order_intake_reads.py` |
| Worker | The gateway flag; a write never reaches a channel; the sync job wired | `tests/worker/test_worker_gateway.py` |
| API | `HTCO_ORDER_SYNC_IN_API` wiring | `tests/api/test_etsy_wiring.py` |
| Integration (Postgres) | As `htco_worker`: ten replays, an older copy, the same row as the API's backfill, no second order table, no contact column, another organization refused | `tests/integration/test_order_intake.py` |
| Database (pgTAP) | The machine's trigger, entry kinds, the worker's column grants and organization scope | `supabase/tests/database/17_order_intake.test.sql` |

### Slice 5.5

| Level | What is covered | Location |
|---|---|---|
| Application | Drifts (status, items, shipments, missing after the grace, none when equal); the drift event without buyer data; the times (default, parsing, which run is due); reconcile and sweep runs (200 receipts alone, a lost webhook, the sweep's open orders of any age, a drift once, the count, the lock, the cursor's error type); the fan-out; the budget from the schedule and the limiter | `tests/application/test_order_reconciliation.py` |
| Worker | The tick known and off unless named; `HTCO_RECONCILIATION_SCHEDULE` (default twice a day, invalid refused); the due run only; each reason's run | `tests/worker/test_reconciliation_jobs.py` |
| Adapter | The open-orders filters, one GET | `tests/integrations/test_etsy_order_intake_reads.py` |
| Integration (Postgres) | A missing order once with its event; a status drift with both values; no drift when equal; the cursor | `tests/integration/test_order_reconciliation.py` |
| Database (pgTAP) | Drift kinds and uniqueness, cursor error types, the worker's organization scope | `supabase/tests/database/18_order_reconciliation.test.sql` |

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-5.3.1 | `test_verify__a_valid_signature_passes`; `test_verify__refusals_name_their_reason`; `test_verify__inside_the_five_minute_window_passes`; `test_verify__any_v1_entry_may_match_during_a_secret_rotation`; `test_ingress__a_replayed_webhook_id_is_ignored`; `test_ingress__nothing_is_stored_before_the_signature_verifies`; `test_webhook__unverified__401_nothing_stored_logged_without_the_body`; `test_webhook__replayed_webhook_id__204_and_ignored`; `test_webhook__over_64_kib__413_before_the_signature_is_checked` |
| AC-5.3.2 | `test_webhook__20_concurrent_valid_webhooks__p95_under_one_second` |
| AC-5.3.3 | `test_limiter__over_the_limit_waits_until_the_window_ends_then_admits`; `test_webhook__over_the_rate_limit__429_with_retry_after_then_accepted` |
| AC-5.3.4 | `test_parse__recorded_payloads_name_their_resource` (11 types); `test_ingress__each_event_type_is_stored_and_routed_to_one_job` (11 types); `test_ingress__an_unknown_event_type_is_stored_and_counted_with_no_job`; `test_process__order_events_trigger_etsy_order_intake_once_per_receipt`; `test_process__listing_events_reread_the_listing_and_publish_it`; `test_process__inventory_events_reread_the_stock_and_publish_it`; `test_process__out_of_stock_is_published_when_the_reread_confirms_it`; `test_webhook_job__rereads_through_the_adapter_with_gets_only`; `test_webhook__routed_to_the_connected_shop_with_one_job`; `test_webhook__unknown_type_or_shop__stored_with_no_job` |
| AC-5.4.1 | `test_the_same_receipt_replayed__writes_nothing_more`; `test_receipt__the_same_event_ten_times__one_order_and_one_ingested_event`; `test_worker__the_same_receipt_ten_times__one_order_one_event_one_history` (Postgres) |
| AC-5.4.2 | `test_an_older_copy_after_a_newer_one__changes_nothing`; `test_worker__an_older_copy_after_a_newer_one__changes_nothing` (Postgres) |
| AC-5.4.3 | `test_counts__cancelled_refunded_and_unpaid_orders_never_count`; `test_counts__units_and_orders_are_counted_separately` (property tests) |
| AC-5.4.4 | `test_money__a_float_anywhere_fails`; `test_money__exact_from_amount_and_divisor`; `test_financial_entries__exact_decimals_with_their_currency` |
| AC-5.4.5 | `test_transition__every_allowed_edge_passes`; `test_transition__every_other_edge_is_refused`; `test_ingest_path__passes_every_state_in_between`; `test_transition_path__a_state_behind_is_refused`; `test_a_new_paid_order__passes_paid_ingested_awaiting_with_one_ingested_event`; `test_a_newer_copy__moves_the_state_and_publishes_an_update`; `test_cancelled_and_refunded__publish_their_own_events`; pgTAP `17_order_intake` |
| AC-5.4.6 | `test_one_store__the_api_sync_and_the_worker_write_the_same_order`; `test_schema__no_second_order_table_exists`; `test_schema__no_column_can_hold_contact_data`; `test_get_order_receipt__fake_contact_values_are_never_kept`; `test_the_hash_and_the_events_carry_no_buyer_data`; `test_receipt__reread_through_the_gateway_and_ingested_with_its_buyer_row` |
| AC-5.5.1 | `test_reconcile__200_receipts_ingested_by_reconciliation_alone` |
| AC-5.5.2 | `test_reconcile__a_receipt_whose_webhook_was_lost__in_the_next_run`; `test_fan_out__one_job_per_shop_and_run_under_the_orders_lock`; `test_due__the_first_run_reconciles_the_last_sweeps`; `test_tick__enqueues_the_due_run_only` |
| AC-5.5.3 | `test_budget__the_pilots_daily_calls_stay_inside_the_app_budget`; `test_budget__the_gateways_limiter_enforces_it` |
| AC-5.5.4 | `test_drifts__status_items_and_shipments_with_both_values`; `test_drifts__a_matching_order_has_none`; `test_drifts__missing_only_once_older_than_the_grace`; `test_drift_event__orders_drift_detected_with_no_buyer_data`; `test_reconcile__a_drift_is_recorded_once_with_its_event`; `test_a_missing_order__one_drift_and_one_event_however_often_reconciled` and `test_a_status_difference__recorded_with_both_values` (Postgres) |
| AC-5.5.5 | `test_a_stale_read__asks_the_queue_and_runs_nothing_here`; `test_the_manual_sync__asks_the_queue_forced_and_is_audited`; `test_main__order_sync_runs_in_the_api_only_as_the_fallback` (in effect once `HTCO_ORDER_SYNC_IN_API` is off at activation) |

## Verification

- Locally: the application, API, adapter and worker tests (`uv run pytest`). The Postgres and pgTAP tests run in CI only (no Docker on the development machine).
- Not verified against Etsy: no test calls the live shop, and the endpoint stays off until the owner registers it.
