# Orders ingest — Security

## Threats and controls

| Threat | Control | Test |
|---|---|---|
| Forged webhook | HMAC-SHA256 over `id.timestamp.body` with the endpoint's secret, constant-time comparison, before anything is parsed or stored (ASVS V4, V2.3) | `test_verify__refusals_name_their_reason`, `test_ingress__nothing_is_stored_before_the_signature_verifies` |
| Replay | 5-minute window on `webhook-timestamp`; `webhook-id` unique in the inbox | `test_verify__inside_the_five_minute_window_passes`, `test_ingress__a_replayed_webhook_id_is_ignored` |
| Oversized body, flood | 64 KiB limit while reading (`Content-Length` and streamed); 429 per source | `test_webhook__over_64_kib__413_before_the_signature_is_checked`, `test_webhook__chunked_body_over_the_limit__413`, `test_webhook__over_the_rate_limit__429_with_retry_after_then_accepted` |
| A payload naming another shop's listing | The re-read checks the listing's shop; a mismatch publishes nothing | `test_process__a_listing_of_another_shop_publishes_nothing` |
| Writes to the live shop | Re-reads are GETs through the guarded adapter (both write guards stay); strict non-GET mocks | `test_get_listing__one_get_with_the_listings_shop`, `test_webhook_job__rereads_through_the_adapter_with_gets_only` |
| Secret leak | `WebhookSecret` has no repr; the settings error never repeats it; stored only as a GitHub secret | `test_secret__parses_whsec_base64_and_hides_itself`, `test_webhooks__an_invalid_secret__refused_without_repeating_it` |
| Personal data in logs | Etsy webhooks carry IDs only; logs carry the event type, reason and IDs, never the body or header values | `test_webhook__unverified__401_nothing_stored_logged_without_the_body` |
| Cross-tenant work | The job runs in the shop's organization; the worker inserts executions of that organization only (RLS) | pgTAP `15_etsy_webhook_intake`, `test_worker__marks_its_events_processed_and_reads_its_shop` |
