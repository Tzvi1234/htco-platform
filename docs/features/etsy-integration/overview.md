# Etsy integration — Overview

| | |
|---|---|
| **Slug** | `etsy-integration` |
| **Phase / slices** | Phase 4 — slice 4.1 (done); 4.2 to 4.7 planned |
| **Requirements** | [FR-SHOP-01 to FR-SHOP-05](../../product/PRD.md) |
| **Decision** | [ADR-0008](../../adr/0008-etsy-integration.md); research in [Etsy API capabilities](../../architecture/etsy-api-capabilities.md) |
| **Status** | In progress (4.1 adapter foundation) |
| **Last updated** | 2026-10-15 |

## What and why

HTCO reads a connected Etsy shop through one port, `MarketplacePort`, so use cases never see Etsy's payloads. Slice 4.1 builds the foundation every later Etsy feature stands on: the `EtsyAdapter` with the first read operations (shop, a page of receipts, a page of listings), a rate-limit budget that keeps HTCO inside Etsy's per-second and per-day limits, retries that are safe for reads, and typed errors that later slices turn into honest messages on the shop card.

No Etsy key exists yet, so everything is tested against hand-written fixtures built from Etsy's OpenAPI spec and documentation, scrubbed of personal data. The first real call happens when the owner connects the pilot shop (slices 4.3 to 4.5).

## Users and actors

- **System:** the order poller and capability probes (Phases 4 and 5) call the port; the shop card (slice 4.6) reads the last seen request budget.
- **Owner:** indirectly, through the shop card and later connection flows.

## Acceptance criteria

| ID | Criterion (Given / When / Then) | Slice | Status |
|---|---|---|---|
| AC-4.1.1 | Given Etsy answers 400, 401, 403, 404, 409, 422, 429 or 5xx, when the adapter calls it, then each status maps to a typed error; 5xx, 429 and transport failures retry with backoff (429 honours `retry-after`), other 4xx do not retry. | 4.1 | Pass |
| AC-4.1.2 | Given Etsy reported fewer requests left today than the configured floor, when a routine (non-urgent) call is made, then it is deferred with `QuotaDeferredError`, a warning is logged and the `htco.etsy.calls_deferred` counter grows; urgent calls still run; the last seen budget is available for the shop card. | 4.1 | Pass at the adapter; the shop card shows it from slice 4.6 |
| AC-4.1.3 | Given any committed fixture, when the test suite runs, then a test fails if it contains an email address, a street address or a token-shaped string. | 4.1 | Pass |

## Scope

- **In (4.1):** `MarketplacePort` and its value types and errors (`htco_application.marketplace`); `EtsyAdapter` (`htco_integrations.etsy`); settings `HTCO_ETSY_KEYSTRING`, `HTCO_ETSY_SHARED_SECRET`, `HTCO_ETSY_DAILY_QUOTA_FLOOR`; fixtures under `tests/fixtures/etsy/` and the fixture hygiene test.
- **Out (4.1):** the token vault (4.2; the adapter asks an `AccessTokenProvider` for each call), OAuth (4.3), refresh (4.4), probes (4.5), the shop card (4.6), wiring the adapter into the API (with 4.3), any Etsy write.

## External facts learned

From Etsy's OpenAPI spec and developer documentation (2026-10-15; not yet confirmed against a live key):

- Base URL `https://openapi.etsy.com/v3/application`; every request carries `x-api-key: <keystring>:<shared secret>` and, for shop data, `Authorization: Bearer <access token>`.
- Rate-limit headers on each answer: `x-limit-per-second`, `x-remaining-this-second`, `x-limit-per-day`, `x-remaining-today`. Limits are per app key, not per shop. 429 carries `retry-after`.
- Errors carry a JSON body `{"error": "<message>"}`.
- Money is `{amount, divisor, currency_code}`; timestamps are Unix seconds (`created_timestamp`, `updated_timestamp`).
- Receipts filter by `min_last_modified` / `max_last_modified` (Unix seconds); lists page with `limit` (1–100, default 25) and `offset`, and answer `{count, results}`.
- Listing states: `active`, `inactive`, `sold_out`, `draft`, `expired`. Receipt `status` arrives capitalised in examples (`Paid`, `Completed`); the adapter lower-cases it.
- **UNCONFIRMED:** the real per-second and per-day limits of our key, and whether the daily window is rolling (the snapshot TTL guards against either).

## Known limits

- A listing state outside Etsy's documented five fails the whole page as a malformed payload; revisit if Etsy adds states.
- The budget is kept in memory per process. With several API or worker processes each sees its own view until a shared budget is needed (Phase 5 workers).
- `review_average` and other shop counters not needed yet are not mapped.
