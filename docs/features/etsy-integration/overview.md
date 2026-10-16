# Etsy integration — Overview

| | |
|---|---|
| **Slug** | `etsy-integration` |
| **Phase / slices** | Phase 4 — slices 4.1 to 4.5 (done at the platform); 4.6 in `htco-clients`; 4.7 disconnect done, restore rehearsal pending |
| **Requirements** | [FR-SHOP-01 to FR-SHOP-05](../../product/PRD.md) |
| **Decision** | [ADR-0008](../../adr/0008-etsy-integration.md); research in [Etsy API capabilities](../../architecture/etsy-api-capabilities.md) |
| **Status** | In progress (4.1 adapter, 4.2 [token vault](token-vault.md), 4.3 and 4.4 [connect flow and refresh](connect-flow.md), 4.5 and 4.7 [operations](operations.md)) |
| **Last updated** | 2026-10-16 |

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
| AC-4.3.1 | A callback with a missing, reused, expired or foreign `state` is rejected and audited. | 4.3 | Pass (application, API and database tests) |
| AC-4.3.2 | Connecting the same Etsy shop twice updates one shop record instead of creating two. | 4.3 | Pass |
| AC-4.3.3 | Connecting does not grant shop access to any user other than the connecting owner. | 4.3 | Pass |
| AC-4.3.4 | Reconnecting with additional scopes (re-consent) updates the granted scopes on the same shop record. | 4.3 | Pass |
| AC-4.4.1 | Ten concurrent requests on an expired token cause exactly one refresh call. | 4.4 | Pass (in process and across two database pools) |
| AC-4.4.2 | A revoked refresh token leads to `needs_reauth`, audited, with no retry loop. | 4.4 | Pass at the platform; the shop card and overview banner show it from slice 4.6 |
| AC-4.5.1 | Probe results are stored with `checked_at` and shown in the UI; a capability never shows as available without a successful probe. | 4.5 | Pass at the platform (stored and served by `GET /v1/shop-integrations`); shown by the shop card in `htco-clients` |
| AC-4.5.2 | The probe never performs a write that changes shop data or notifies a buyer. | 4.5 | Pass: only GETs; non-GET refused by the adapter and by a client hook, proven with a strict HTTP mock |
| AC-4.7.1 | After disconnect, no Etsy call is made for that shop (strict HTTP mock). | 4.7 | Pass |
| AC-4.7.2 | Restore rehearsal record exists in `operations.md` before the real shop is connected in production. | 4.7 | Open: procedure written; the rehearsal itself needs the owner (Free plan has no automatic backups) |

## Scope

- **In (4.1):** `MarketplacePort` and its value types and errors (`htco_application.marketplace`); `EtsyAdapter` (`htco_integrations.etsy`); settings `HTCO_ETSY_KEYSTRING`, `HTCO_ETSY_SHARED_SECRET`, `HTCO_ETSY_DAILY_QUOTA_FLOOR`; fixtures under `tests/fixtures/etsy/` and the fixture hygiene test.
- **Read only, always:** HTCO never writes to Etsy; see [the read-only rule](operations.md#the-read-only-rule).
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

OAuth (developers.etsy.com/documentation/essentials/authentication and the OpenAPI spec, read 2026-10-15; to confirm with the first live connection):

- Authorization at `https://www.etsy.com/oauth/connect` with `response_type=code`, `client_id` (the keystring), `redirect_uri`, space-separated `scope`, `state`, `code_challenge`, `code_challenge_method=S256`.
- Token endpoint `POST https://api.etsy.com/v3/public/oauth/token` (the spec also names `openapi.etsy.com`), form-encoded: `authorization_code` with `client_id`, `redirect_uri`, `code`, `code_verifier`; `refresh_token` with `client_id`, `refresh_token`. The answer: `access_token` prefixed with the numeric user ID and a dot, `token_type` `Bearer`, `expires_in` 3600, `refresh_token` (90 days), sometimes `scope`.
- `GET /v3/application/users/{user_id}/shops` (`getShopByOwnerUserId`) returns the user's shop; 404 when the account has no shop. `getMe` (`shops_r`) would also give `shop_id`, but needs a second call for the name.
- **UNCONFIRMED:** whether a refresh returns a new refresh token every time and whether that resets the 90 days (the platform stores whatever comes back and keeps the old expiry when the token is unchanged); the exact error body of a revoked refresh token (assumed `{"error": "invalid_grant"}` with 400, as in OAuth 2.0).

## Known limits

- A listing state outside Etsy's documented five fails the whole page as a malformed payload; revisit if Etsy adds states.
- The budget is kept in memory per process. With several API or worker processes each sees its own view until a shared budget is needed (Phase 5 workers).
- `review_average` and other shop counters not needed yet are not mapped.
