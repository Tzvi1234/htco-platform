# Etsy integration — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Application | Money exactness, page arithmetic, quota snapshot | `tests/application/test_marketplace.py` |
| Adapter contract | Requests (URL, query, `x-api-key`, Bearer, timeout), payload mapping, every error status, retries and backoff, `retry-after` forms, throttle, daily floor, logs and metrics, configuration | `tests/integrations/test_etsy_adapter.py` |
| Fixture hygiene | Every file in `tests/fixtures/` scanned; the detectors tested on real-looking and fake values | `tests/integrations/test_fixture_hygiene.py` |
| Settings | Etsy secrets optional, paired, hidden in `repr`, blank means unset, floor not negative | `tests/api/test_settings.py` |
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

## Verification evidence

### Slice 4.1 — 2026-10-15

- **Outcome:** the application can read a shop, a page of receipts and a page of listings through `MarketplacePort`, within Etsy's rate limits, with typed failures.
- **Automated:** `uv run ruff format --check`, `uv run ruff check .`, `uv run mypy` (strict, whole workspace), `uv run lint-imports` (5 contracts kept, including the new adapter layer) and `uv run pytest -q` all pass locally; CI runs the same plus coverage floors.
- **Real run:** not possible yet: no Etsy key exists (Phase 0 prerequisite). Every documented status, header and payload shape is replayed from fixtures instead. The first live, read-only call (with the owner's approval) belongs to slices 4.3 to 4.5 and will be recorded and scrubbed as a new fixture if the shape differs.
- **Secrets in logs:** the quota tests capture every JSON log line and assert the keystring, shared secret and access token never appear; error messages and `repr` are checked the same way.
