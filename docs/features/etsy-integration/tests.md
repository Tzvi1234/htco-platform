# Etsy integration — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Application | Money exactness, page arithmetic, quota snapshot | `tests/application/test_marketplace.py` |
| Adapter contract | Requests (URL, query, `x-api-key`, Bearer, timeout), payload mapping, every error status, retries and backoff, `retry-after` forms, throttle, daily floor, logs and metrics, configuration | `tests/integrations/test_etsy_adapter.py` |
| Fixture hygiene | Every file in `tests/fixtures/` scanned; the detectors tested on real-looking and fake values | `tests/integrations/test_fixture_hygiene.py` |
| Settings | Etsy secrets optional, paired, hidden in `repr`, blank means unset, floor not negative | `tests/api/test_settings.py` |
| Dashboard reads (adapter) | Every new read is a GET with the connection's token (strict mock), payload mapping, buyer name shortened, no name, tracking number, email, address or review text in `repr` or logs | `tests/integrations/test_etsy_dashboard_reads.py` |
| Dashboard cache | Fresh for the TTL, one load for concurrent callers, stale on passing failures, cooldown, `max_stale` sweep, lasting failures drop the copy, `forget` | `tests/application/test_shop_data_cache.py` |
| Dashboard use cases | Authorization before any call, not connected without a call, pinned tokens, sales by the caller's day, order states and overdue, buyer details by role, filters and pages, stale and unavailable, reconnect and disconnect | `tests/application/test_shop_dashboard.py` |
| Order periods and statistics | Completed orders older than 30 days appear under the longer periods; no status filter; sequential pages up to each period's cap with `partial`; a cache entry per period (30 minutes) and for the statistics basis (6 hours, no buyer data); totals, average, months in the caller's time zone, best month, top products, countries; aggregates only for every role | `tests/application/test_shop_dashboard_history.py`, `tests/api/test_shop_history_routes.py` |
| History reads (adapter) | All time sends no `min_created` and no status filter; country only of the address; the all-time orders and statistics through the real adapter against a strict mock (GET only, offsets 0, 100, 200) | `tests/integrations/test_etsy_receipt_history.py` |
| Dashboard API | Shapes, `no-store` on orders, buyer details by role, 404/409/422/503 problems with `Retry-After`, stale answers, nothing in the logs | `tests/api/test_shop_data_routes.py` |
| Architecture | `htco_integrations` is an adapter beside persistence and the agent runtime | `pyproject.toml` import-linter contracts, `tests/architecture/test_workspace_packages.py` |

Fixtures (`tests/fixtures/etsy/`): `shop.json`, `receipts_page.json`, `listings_page.json` and `errors.json`, written by hand from Etsy's OpenAPI spec with obviously fake IDs, names and amounts. They are served by `httpx.MockTransport`; a fake clock replaces every sleep, so no test waits or reaches the network. `respx` is not a dependency; MockTransport covers the same ground.

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-4.1.1 | `test_client_error__maps_to_its_typed_error__and_is_not_retried` (400, 401, 403, 404, 409, 422), `test_server_error__is_retried_with_backoff__then_succeeds` (500, 502, 503, 504), `test_server_error__every_attempt__raises_unavailable_after_the_last_attempt`, `test_transport_error__is_retried`, `test_transport_error__every_attempt__raises_unavailable`, `test_rate_limited__waits_retry_after__then_succeeds`, `test_rate_limited__retry_after_as_http_date__is_honoured`, `test_rate_limited__without_usable_retry_after__backs_off`, `test_rate_limited__every_attempt__raises_rate_limited_with_retry_after`, `test_rate_limited__retry_after_beyond_the_cap__raises_without_waiting`, `test_errors__never_carry_credentials_or_tokens` |
| AC-4.1.2 | `test_daily_quota_below_floor__defers_routine_calls__with_warning_and_metric`, `test_daily_quota_below_floor__is_logged_once_when_first_seen`, `test_daily_quota_below_floor__urgent_calls__still_run`, `test_daily_quota_at_or_above_floor__routine_calls_run`, `test_daily_quota_below_floor__stale_snapshot__lets_a_routine_call_refresh_it`, `test_daily_quota_floor_zero__never_defers`, `test_quota_headers__headerless_failure__does_not_lift_deferral`, `test_daily_quota_below_floor__stale_snapshot__admits_one_refresh_at_a_time`, `test_daily_quota_below_floor__failed_refresh__frees_the_next_refresh`, `test_quota_headers__are_kept_as_the_last_seen_snapshot` (the value the shop card will show) |
| AC-4.1.3 | `test_committed_fixture__is_scrubbed` (one case per fixture file), `test_find_unscrubbed__real_looking_values__are_reported`, `test_find_unscrubbed__fake_and_structural_values__pass` |

## Edge cases considered

- **Empty, missing, null:** empty pages (`next_offset` None); null `listing_id` and `sku` on receipt lines; missing or garbled quota headers keep calls working; missing fields and non-object JSON become `MarketplaceResponseError`.
- **Limits:** `limit` 0 and 101 refused, 1 and 100 accepted; negative offset refused; `remaining_today` exactly at the floor does not defer, one below does; floor 0 never defers.
- **Time:** naive datetimes refused; `min_last_modified` after `max_last_modified` refused; `retry-after` as seconds, HTTP date, negative or garbage; snapshot TTL boundary.
- **429 with and without `retry-after`, 5xx, timeouts, refused connections, protocol errors:** all covered; a `retry-after` beyond the cap fails at once.
- **Money:** exact `Decimal` from amount and divisor, zero and negative amounts, non-decimal divisors; non-positive divisors and non-ISO currencies refused.
- **Concurrency:** the throttle holds a lock so concurrent calls share one window; a single-process budget is a documented limit (overview).
- **Hostile input:** malformed payloads, timestamps outside the datetime range; no redirects followed.
- **Not applicable in 4.1:** tenancy and roles (no endpoint yet), writes and idempotency (no write), webhooks (Phase 5).

Slices 4.2 to 4.4 have their own test maps in [token vault](token-vault.md#tests) and [connect flow](connect-flow.md#tests).

### Slices 4.5 and 4.7

| Acceptance criterion | Tests |
|---|---|
| AC-4.5.1 | `tests/application/test_shop_capabilities.py` (`test_probe__everything_readable__*`, `test_probe__a_failed_read__never_available`, `test_probe__webhooks_configured__still_unknown_until_one_arrives`, `test_probe__scope_missing__*`, `test_probe__token_refused__*`), `tests/domain/test_integrations.py::test_capability_state__never_available_without_a_successful_probe`, `tests/api/test_capability_disconnect_routes.py::test_recheck__owner__probes_and_the_card_shows_results_with_checked_at`, `tests/integration/test_capabilities_disconnect.py::test_capability_store__*`, pgTAP `07_capabilities_disconnect` |
| AC-4.5.2 | `tests/integrations/test_etsy_read_only.py` (every write method refused by the adapter and by the client hook with a strict mock that fails on any non-GET; no code sets the allow flag; the address check reads one receipt with GET and keeps no value), `test_probe__reads_one_receipt_and_one_listing_urgently_and_nothing_else` |
| AC-4.7.1 | `tests/application/test_shop_disconnect.py::test_after_disconnect__no_etsy_call_is_made_for_the_shop` (real adapter, strict HTTP mock), `tests/api/test_capability_disconnect_routes.py::test_disconnect__afterwards_no_marketplace_call_for_the_shop`, `tests/integration/test_capabilities_disconnect.py::test_disconnect__tokens_gone_shop_disconnected_purge_date_and_audit_at_once` |
| AC-4.7.2 | Procedure in [operations](operations.md#backups-and-the-restore-rehearsal-ac-472); rehearsal record pending (owner) |


## Verification evidence

### Slice 4.6, live dashboard data — 2026-10-22

- **Automated:** `ruff format --check`, `ruff check`, `mypy` (strict), `lint-imports`, the architecture tests and the new adapter, cache, use-case and API tests pass locally; the committed OpenAPI was regenerated; CI runs the whole suite and the contract fuzzing.
- **Real run:** none against Etsy, deliberately: the agent never calls the live API. All data in tests is invented (`tests/shop_data_fakes.py`, new fixtures under `tests/fixtures/etsy/`, scanned by the hygiene test). The first real reads happen when the owner opens the dashboard after deployment; they are GETs only.

### Slices 4.5 and 4.7 — 2026-10-16

- **Automated:** `ruff format --check`, `ruff check`, `mypy` (strict), `lint-imports` and `pytest` pass locally; database tests (`tests/integration`, pgTAP `07`) run in CI's `database` job (Docker was not available locally).
- **Real run:** none against Etsy, deliberately: the agent never calls the real API. The probes' first real run is the owner's next connection or "Re-check"; they only send GETs.
- **Restore rehearsal:** not performed (needs Docker and the production connection string, both with the owner); see the record in [operations](operations.md#rehearsal-record).

### Slice 4.1 — 2026-10-15

- **Outcome:** the application can read a shop, a page of receipts and a page of listings through `MarketplacePort`, within Etsy's rate limits, with typed failures.
- **Automated:** `uv run ruff format --check`, `uv run ruff check .`, `uv run mypy` (strict, whole workspace), `uv run lint-imports` (5 contracts kept, including the new adapter layer) and `uv run pytest -q` all pass locally; CI runs the same plus coverage floors.
- **Real run:** not possible yet: no Etsy key exists (Phase 0 prerequisite). Every documented status, header and payload shape is replayed from fixtures instead. The first live, read-only call (with the owner's approval) belongs to slices 4.3 to 4.5 and will be recorded and scrubbed as a new fixture if the shape differs.
- **Secrets in logs:** the quota tests capture every JSON log line and assert the keystring, shared secret and access token never appear; error messages and `repr` are checked the same way.
