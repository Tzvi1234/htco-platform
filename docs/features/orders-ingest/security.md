# Orders ingest — Security

## Threats and controls

| Threat | Control | Test |
|---|---|---|
| Forged webhook | HMAC-SHA256 over `id.timestamp.body` with the endpoint's secret, constant-time comparison, before anything is parsed or stored (ASVS V4, V2.3) | `test_verify__refusals_name_their_reason`, `test_ingress__nothing_is_stored_before_the_signature_verifies` |
| Replay | 5-minute window on `webhook-timestamp`; `webhook-id` unique in the inbox | `test_verify__inside_the_five_minute_window_passes`, `test_ingress__a_replayed_webhook_id_is_ignored` |
| Oversized body, flood | 64 KiB limit while reading (`Content-Length` and streamed); 429 per IP for failures, and Etsy-wide only for verified deliveries | `test_webhook__over_64_kib__413_before_the_signature_is_checked`, `test_webhook__chunked_body_over_the_limit__413`, `test_webhook__over_the_rate_limit__429_with_retry_after_then_accepted`, `test_webhook__oversize_and_bad_timestamp__do_not_spend_the_etsy_wide_budget`, `test_webhook__signed_delivery_passes_while_its_own_ip_is_flooded` |
| A payload naming another shop's listing | The re-read checks the listing's shop; a mismatch publishes nothing | `test_process__a_listing_of_another_shop_publishes_nothing` |
| Writes to the live shop | Re-reads are GETs through the guarded adapter (both write guards stay); strict non-GET mocks | `test_get_listing__one_get_with_the_listings_shop`, `test_webhook_job__rereads_through_the_adapter_with_gets_only` |
| Secret leak | `WebhookSecret` has no repr; the settings error never repeats it; stored only as a GitHub secret | `test_secret__parses_whsec_base64_and_hides_itself`, `test_webhooks__an_invalid_secret__refused_without_repeating_it` |
| Personal data in logs | Etsy webhooks carry IDs only; logs carry the event type, reason and IDs, never the body or header values | `test_webhook__unverified__401_nothing_stored_logged_without_the_body` |
| Cross-tenant work | The job runs in the shop's organization; the worker inserts executions of that organization only (RLS) | pgTAP `15_etsy_webhook_intake`, `test_worker__marks_its_events_processed_and_reads_its_shop` |
| Buyer data through the gateway (5.4; least privilege, AUD-06) | The order history form of `etsy.receipts.read` answers only automation actors, and only Etsy Order Intake (`BUYER_DATA_AUTOMATIONS`) gets the buyer's name and shipping address in it; tracking codes go only to Intake and Tracking Sync (`TRACKING_CODE_AUTOMATIONS`). Order Deduplication & Verification and any automation granted the action later get the same receipts without them. A user or an AI employee gets 403. It is an R1 output, never stored with the action, never in events or logs | `test_channel__the_order_history_form_is_the_automations_alone`, `test_order_history_form__buyer_name_and_address_reach_etsy_order_intake_only`, `test_receipt__reread_through_the_gateway_and_ingested_with_its_buyer_row` |
| Contact data (5.4) | The adapter never reads it; no column can hold it | `test_get_order_receipt__fake_contact_values_are_never_kept`, `test_schema__no_column_can_hold_contact_data` |
| The worker reading money or buyer values (5.4) | Column-level grants: order state, times, hash, flags and item quantities only; the buyer row is written and replaced, never read | pgTAP `17_order_intake` |
| A write from an automation (5.4) | The worker's gateway waits for an approval no one can give there; the channel is never called | `test_write__through_the_worker_parts__waits_and_never_reaches_a_channel` |
| Drift values (5.5) | Statuses, item IDs and quantities, shipment carriers and times only; no money, name, address or tracking number | `test_drift_event__orders_drift_detected_with_no_buyer_data`, pgTAP `18_order_reconciliation` |
| Reconciliation's Etsy load (5.5) | Every read passes the gateway's app-wide limiter; past the budget a read is deferred and Etsy is not called | `test_budget__the_gateways_limiter_enforces_it` |
| Cross-tenant ingest (5.4) | RLS on every table in the job's organization | `test_worker__another_organizations_scope_writes_nothing`, pgTAP `17_order_intake` |
