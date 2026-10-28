# Phase 4 — Etsy shop connection

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phases 2 and 3; Etsy Seller App registered (Phase 0) |
| **Requirements** | FR-ADM-02, FR-SHOP-01, FR-SHOP-02, FR-SHOP-03, FR-SHOP-04, FR-SHOP-05 |
| **Status** | Partial (audit 2026-10-28). Built and connected to LeatherPY in production. Daily backups and the restore rehearsal (AC-4.7.2) are **deferred by owner until first sale** (decision of 2026-10-28, [ASSUMPTIONS](../../product/ASSUMPTIONS.md) D-28): the project stays on the Supabase Free plan, which has no automatic backups, until the product is sold to a customer; the backup infrastructure is planned for Supabase Pro (daily backups or PITR) with a restore runbook, activated and tested then. Also open: the remaining daily quota on the shop card (AC-4.1.2), the probing and error screenshot states (AC-4.6.2) and the exit gate |

## Progress

- [ ] 4.1 Etsy adapter foundation (partial, audit 2026-10-28: AC-4.1.1 and 4.1.3 pass; open AC-4.1.2, the remaining daily quota is read inside the adapter but no API returns it and the shop card does not show it; in progress, see Phase 6's note of 2026-10-28)
- [x] 4.2 Token vault
- [x] 4.3 OAuth connect flow ([connect flow](../../features/etsy-integration/connect-flow.md))
- [x] 4.4 Token refresh (platform; the shop card warning and overview banner are shown by slice 4.6)
- [x] 4.5 Capability probes (platform; the shop card shows them in `htco-clients`; [operations](../../features/etsy-integration/operations.md#capability-probes))
- [x] 4.6 Shops UI (the shop card in `htco-clients`), and the shop's live data on the dashboard: Shops, Home, Orders, Products, Shipping and Customers read Etsy through read-only endpoints ([dashboard data](../../features/etsy-integration/dashboard-data.md))
- [ ] 4.7 Disconnect and data protection baseline (disconnect done at the platform; the restore rehearsal waits for the owner, see [operations](../../features/etsy-integration/operations.md#rehearsal-record))

**4.6 is partial (audit 2026-10-28).** The shop card and the live data are built, but AC-4.6.2's screenshots cover four states (ok, expiring, needs re-auth, disconnected), not probing and error, and the card does not show the quota of AC-4.1.2. 4.7's "daily backups confirmed" and its restore rehearsal are deferred by owner until first sale ([ASSUMPTIONS](../../product/ASSUMPTIONS.md) D-28): see the status line.

## Goal

The owner connects the real Etsy shop from the web app and sees, honestly, what HTCO is allowed to do with it: granted scopes, verified capabilities, token health. Tokens are encrypted, refreshed and revocable. From this phase on, real business data exists, so the data protection baseline must be in place first.

## Scope

**In:** Etsy adapter foundation (HTTP client, rate-limit budget, typed errors, scrubbed fixtures), token vault with envelope encryption, OAuth PKCE connect and callback, on-demand refresh with a per-shop lock, capability probes, shop card UI, disconnect and revocation, data protection baseline (backups verified, restore rehearsed).

**Out:** order ingest (Phase 5), any Etsy write.

## Owner prerequisites

Research of 2026-10-15 ([Etsy API capabilities](../../architecture/etsy-api-capabilities.md), [multi-shop policy](../../architecture/etsy-multi-shop-policy.md)):
- One HTCO app connects every shop through OAuth, with no proxies and never a second key.
- Owner, 2026-10-15: we start with the owner's existing **Seller App** (his own shop only). Before connecting other people's shops we move to a **Personal App**, and later to Commercial Access. The code does not change; only the keystring and the app's access level do.
- Personal access covers the owner's shops plus about 4 others. Commercial access is requested early, describing the AI assistance honestly.
- The first test with our own key checks `createReceiptShipment` and the receipt addresses for an Israeli shop, which Etsy restricted for new keys on 2026-07-20. The result decides the Phase 6 tracking path: direct upload, an approved POD or fulfillment partner, or a task for the owner.

- Etsy Seller App keystring and shared secret stored as secrets; redirect URI `https://dash.ht-co.app/integrations/etsy/callback` registered exactly (a web-app route; see ADR-0008).
- The owner has read Etsy's current API Terms (A-21).

## Slices

### 4.1 — Etsy adapter foundation

`MarketplacePort` with the first read operations (shop, receipts page, listing page); `EtsyAdapter` on httpx with timeouts, `x-api-key` header, rate-limit budget read from `x-remaining-*` headers (throttle at half the per-second limit), backoff on 429 honoring `retry-after`, typed errors for 401/403/404/409/429/5xx; scrubbed fixtures (hand-written from the OpenAPI spec until a key exists; replaced by recorded ones from the first live calls). Built in `packages/integrations` (`htco_integrations.etsy`); see [etsy-integration](../../features/etsy-integration/overview.md).

| ID | Acceptance criterion |
|---|---|
| AC-4.1.1 | Each documented error status maps to a typed error; 5xx and 429 retry with backoff, 4xx do not. |
| AC-4.1.2 | When `x-remaining-today` falls below the configured floor, non-urgent calls are deferred, a warning is logged with a metric, and the shop card shows the remaining quota. |
| AC-4.1.3 | A test fails if any committed fixture contains an email address, a street address or a token-shaped string. |

### 4.2 — Token vault

AES-GCM envelope encryption with a key from the environment and a key ID for rotation; `shop_integrations` stores ciphertext, scopes, expiry times and status.

| ID | Acceptance criterion |
|---|---|
| AC-4.2.1 | Reading `shop_integrations` directly in SQL shows no plaintext token. |
| AC-4.2.2 | Rotating the encryption key re-encrypts stored tokens without downtime (tested with two keys). |
| AC-4.2.3 | Tokens never appear in logs, errors or API responses (log-capture test). |

### 4.3 — OAuth connect flow

`POST /v1/shops/etsy/connect` (owner, MFA, recent re-authentication) returns the Etsy authorization URL with `state` and a PKCE challenge, stored server-side for 10 minutes and bound to the owner's session. Etsy redirects the browser to the web route `/integrations/etsy/callback`, which posts `code` and `state` to `POST /v1/integrations/etsy/callback` as a same-site request, so the `SameSite=Strict` session cookie is sent. The API validates `state` against that session, exchanges the code, stores tokens, creates or updates the `shops` row, grants the owner access and writes audit. Requested scopes: `shops_r`, `transactions_r`, `listings_r`; later phases add scopes by reconnecting (re-consent; see the scope table in ADR-0008).

| ID | Acceptance criterion |
|---|---|
| AC-4.3.1 | A callback with a missing, reused, expired or foreign `state` is rejected and audited. |
| AC-4.3.2 | Connecting the same Etsy shop twice updates one shop record instead of creating two. |
| AC-4.3.3 | Connecting does not grant shop access to any user other than the connecting owner. |
| AC-4.3.4 | Reconnecting with additional scopes (re-consent) updates the granted scopes on the same shop record. |

### 4.4 — Token refresh

Refresh when the access token has under five minutes left, under a per-shop advisory lock; always store the newest refresh token; show a warning on the shop card 10 days before the refresh token's 90-day expiry (scheduled checks arrive in Phase 5, email in Phase 6); mark the integration `needs_reauth` after three failed refreshes.

| ID | Acceptance criterion |
|---|---|
| AC-4.4.1 | Ten concurrent requests on an expired token cause exactly one refresh call. |
| AC-4.4.2 | A revoked refresh token leads to `needs_reauth`, shown on the shop card and an overview banner, audited, with no retry loop. |

### 4.5 — Capability probes

At connection and on demand (a "Re-check" button; daily scheduling arrives with the job scheduler in Phase 5): granted scopes; shop details; webhook availability (from configuration until Etsy exposes it by API); shipment write permission checked without creating a shipment (method confirmed in the Phase 0 spike; if no safe probe exists, the capability stays `unknown` and writes stay disabled); address field availability.

| ID | Acceptance criterion |
|---|---|
| AC-4.5.1 | Probe results are stored with `checked_at` and shown in the UI; a capability never shows as available without a successful probe. |
| AC-4.5.2 | The probe never performs a write that changes shop data or notifies a buyer. |

### 4.6 — Shops UI

Shop card: connection status, scopes, capabilities with plain-language explanations, last successful call, token health; connect, reconnect and disconnect buttons with confirmation dialogs.

| ID | Acceptance criterion |
|---|---|
| AC-4.6.1 | "Capability blocked" states explain the allowed alternative (for example, "Copy the tracking number into Etsy; HTCO reads it there"; AutoVA does not write tracking to Etsy, A-05). |
| AC-4.6.2 | All states (connected, needs re-auth, disconnected, probing, error) have screenshot tests in he/en × light/dark. |

### 4.7 — Disconnect and data protection baseline

Disconnect deletes the tokens (and revokes them at Etsy if Etsy offers a way, A-15), stops sync, marks the shop disconnected and sets a purge date 30 days ahead (the purge job runs from Phase 5; records under legal retention are kept); daily backups confirmed; a restore rehearsal into a scratch project documented.

| ID | Acceptance criterion |
|---|---|
| AC-4.7.1 | After disconnect, no Etsy call is made for that shop (verified by a test with a strict HTTP mock). |
| AC-4.7.2 | Restore rehearsal record exists in `docs/features/etsy-integration/operations.md` before the real shop is connected in production. |

## Test plan

Adapter contract tests against fixtures; vault crypto tests; OAuth flow tests with a fake Etsy authorization server; concurrency test for refresh; UI tests; manual verification connecting the real shop in production with the owner present.

## Security checklist

- [x] ASVS V10 (OAuth client): state, PKCE S256, exact redirect URI, minimal scopes (10.2.3). See [connect flow security](../../features/etsy-integration/connect-flow.md#security).
- [x] Tokens encrypted at rest at the application level; key rotation tested.
- [x] Connect and disconnect require owner role, MFA and recent re-authentication; both audited (the `CONNECT_SHOP` capability, `shop.connected` and `shop.disconnected` audit records; ticked by the audit of 2026-10-28).
- [ ] Etsy trademark notice present in the web app's footer or about page.

## Documentation

`docs/features/etsy-integration/` (overview with Phase 0 findings, design, tests, security, operations); ADR-0008 validation section updated with real capabilities.

## Demo

Connect the pilot shop; show the capability report; disconnect and reconnect; show the audit trail.

## Exit gate

- [ ] All AC-4.x pass; the pilot shop connected in production; restore rehearsal recorded.

## Rollback

Disconnect the shop (revokes tokens); code rollback leaves encrypted tokens intact.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy denies or limits the Seller App | Personal App application in parallel (Phase 0) |
| No safe probe for shipment write permission | Keep the capability `unknown` and writes disabled; tracking comes from AutoVA through Etsy anyway |

## Cost impact

None beyond existing services.
