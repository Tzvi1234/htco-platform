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

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-5.3.1 | `test_verify__a_valid_signature_passes`; `test_verify__refusals_name_their_reason`; `test_verify__inside_the_five_minute_window_passes`; `test_verify__any_v1_entry_may_match_during_a_secret_rotation`; `test_ingress__a_replayed_webhook_id_is_ignored`; `test_ingress__nothing_is_stored_before_the_signature_verifies`; `test_webhook__unverified__401_nothing_stored_logged_without_the_body`; `test_webhook__replayed_webhook_id__204_and_ignored`; `test_webhook__over_64_kib__413_before_the_signature_is_checked` |
| AC-5.3.2 | `test_webhook__20_concurrent_valid_webhooks__p95_under_one_second` |
| AC-5.3.3 | `test_limiter__over_the_limit_waits_until_the_window_ends_then_admits`; `test_webhook__over_the_rate_limit__429_with_retry_after_then_accepted` |
| AC-5.3.4 | `test_parse__recorded_payloads_name_their_resource` (11 types); `test_ingress__each_event_type_is_stored_and_routed_to_one_job` (11 types); `test_ingress__an_unknown_event_type_is_stored_and_counted_with_no_job`; `test_process__order_events_trigger_etsy_order_intake_once_per_receipt`; `test_process__listing_events_reread_the_listing_and_publish_it`; `test_process__inventory_events_reread_the_stock_and_publish_it`; `test_process__out_of_stock_is_published_when_the_reread_confirms_it`; `test_webhook_job__rereads_through_the_adapter_with_gets_only`; `test_webhook__routed_to_the_connected_shop_with_one_job`; `test_webhook__unknown_type_or_shop__stored_with_no_job` |

## Verification

- Locally: the application, API, adapter and worker tests (`uv run pytest`). The Postgres and pgTAP tests run in CI only (no Docker on the development machine).
- Not verified against Etsy: no test calls the live shop, and the endpoint stays off until the owner registers it.
