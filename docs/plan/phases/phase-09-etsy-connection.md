# Phase 9 — Etsy connection and live orders

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 4](phase-04-durable-core-order-intake.md); Etsy Seller App registered ([Phase 0](phase-00-groundwork.md)). May run in parallel with Phases 6–8 ([PLAN §2](../PLAN.md#2-phases-and-milestones)) |
| **Requirements** | FR-ADM-02, FR-ORD-01, FR-ORD-02, FR-ORD-09, FR-SHOP-01, FR-SHOP-02, FR-SHOP-03, FR-SHOP-04, FR-SHOP-05 |
| **Mandatory tests owned** | Test 1 on real data ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); Phase 4 owns it on recorded fixtures |
| **Status** | Not started |

## Progress

- [ ] 9.1 Etsy adapter foundation
- [ ] 9.2 Token vault
- [ ] 9.3 OAuth connect flow
- [ ] 9.4 Token refresh
- [ ] 9.5 Capability probes
- [ ] 9.6 Shops UI
- [ ] 9.7 Disconnect and data protection baseline
- [ ] 9.8 Live order intake: real webhooks and reconciliation
- [ ] 9.9 Sync health on real data

## Goal

The owner connects the real Etsy shop from the web app and sees, honestly, what HTCO is allowed to do with it: granted scopes, verified capabilities, token health. Tokens are encrypted, refreshed and revocable. Then the order intake built in [Phase 4](phase-04-durable-core-order-intake.md) against signed recorded fixtures is switched to the real shop: real signed webhooks enter the inbox, the reconciliation poller reads the real Etsy API, and every paid order appears in HTCO exactly once. From this phase on, real business data exists, so the data protection baseline must be in place first.

No AI employee and no model call is part of this phase. Order work stays with the three deterministic automations activated in Phase 4 (Etsy Order Intake, Order Deduplication & Verification, Product Sales Counters).

## Why now

Phase 4 proved the queue, inbox, outbox, engine and order automations with fixtures, so connecting the real shop only swaps the source of the data. Fulfillment monitoring ([Phase 10](phase-10-fulfillment-incidents.md)) needs real orders and real shipment records to be useful, and Eitan's evaluation set needs real-shape exceptions. Phase 9 needs nothing from the workforce runtime, so it can run alongside Phases 6–8 if the owner wants live orders earlier.

## Scope

**In:** Etsy adapter foundation (HTTP client, rate-limit budget, typed errors, scrubbed fixtures); token vault with envelope encryption; OAuth PKCE connect and callback; on-demand refresh with a per-shop lock; capability probes; shop card UI; disconnect and revocation; data protection baseline (backups verified, restore rehearsed against the Phase 4 drill procedure); switching Phase 4's webhook ingress and reconciliation from fixtures to the real shop, with an initial backfill and a shadow period; sync health metrics on real data; mandatory test 1 on real data.

**Out:** tracking detection, SLA and incidents ([Phase 10](phase-10-fulfillment-incidents.md)); any Etsy write (the guarded shipment write is Phase 10, listing writes are [Phase 11](phase-11-listing-health.md)); finance ledger endpoints ([Phase 13](phase-13-procurement-finance.md)); AutoVA integration (Phases 10 and 13).

## Owner prerequisites

- Etsy Seller App keystring and shared secret stored as secrets; redirect URI `https://app.ht-co.app/integrations/etsy/callback` registered exactly (a web-app route; see [ADR-0008](../../adr/0008-etsy-integration.md)).
- Webhook endpoint registered in the Etsy developer portal for the app, if Etsy offers webhooks for it, and the webhook signing secret stored as a secret.
- The owner has read Etsy's current API Terms (A-09).
- The owner is present for the first production connection and for the test 1 run on real data.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. Load `design-system` before any work in `htco-clients` (slice 9.6).
2. Read this file, then [PLAN.md](../PLAN.md), [ADR-0008](../../adr/0008-etsy-integration.md), [ADR-0007](../../adr/0007-job-queue.md), [ADR-0013](../../adr/0013-automation-engine.md), the Phase 4 file (webhook ingress, receipt ingest, reconciliation and drill slices), `docs/architecture/DATA_MODEL.md` (`shops`, `shop_integrations`, `sync_cursors`, `sync_drifts`), `EVENT_CATALOG.md` and `docs/features/etsy-integration/` from Phase 0.
3. Refresh the facts at kickoff: Etsy Open API v3 rate limits, webhook availability and signature scheme for Seller Apps, token lifetimes, scope names. Record what changed in ADR-0008's validation log. Never rely on remembered values.
4. Work in this order: 9.1 → 9.2 → 9.3 → 9.4 → 9.5 → 9.7 (the restore rehearsal must be recorded before the real shop is connected in production) → 9.6 → 9.8 → 9.9.
5. Etsy code lives only in `packages/integrations` behind `MarketplacePort`. Use cases in `packages/application` never import the adapter.
6. Write failing tests first against recorded, scrubbed fixtures (respx). The only live calls are the manual verification steps listed in the test plan, made with the owner present.
7. Never: request a scope this phase does not need; write anything to Etsy; log or return a token; commit a fixture with buyer data or a token-shaped string; disable signature verification to "make webhooks work"; turn on the webhook path in production before the shadow period in 9.8 is clean.
8. Stop and ask the owner when Etsy refuses or limits the Seller App, when webhooks are not available for the app (9.8 then runs on reconciliation alone and test 1 is adapted, see 9.8), when the shadow period shows any order missing or duplicated, or when a capability probe would need a write.

## Slices

### 9.1 — Etsy adapter foundation

`MarketplacePort` with the first read operations (shop, receipts page, receipt, listing page); `EtsyAdapter` on httpx with timeouts, `x-api-key` header, rate-limit budget read from `x-remaining-*` headers (throttle at half the per-second limit), backoff on 429 honoring `retry-after`, typed errors for 401/403/404/409/429/5xx; respx fixtures recorded in the Phase 0 spike and scrubbed. Phase 4 used a fixture adapter of the same port; this slice makes the real one pass the same contract tests.

| ID | Acceptance criterion |
|---|---|
| AC-9.1.1 | Each documented error status maps to a typed error; 5xx and 429 retry with backoff, 4xx do not. |
| AC-9.1.2 | When `x-remaining-today` falls below the configured floor, non-urgent calls are deferred, a warning is logged with a metric, and the shop card shows the remaining quota. |
| AC-9.1.3 | A test fails if any committed fixture contains an email address, a street address or a token-shaped string. |
| AC-9.1.4 | The Phase 4 fixture adapter and `EtsyAdapter` pass the same `MarketplacePort` contract test suite. |

### 9.2 — Token vault

AES-GCM envelope encryption with a key from the environment and a key ID for rotation; `shop_integrations` stores ciphertext, scopes, expiry times and status.

| ID | Acceptance criterion |
|---|---|
| AC-9.2.1 | Reading `shop_integrations` directly in SQL shows no plaintext token. |
| AC-9.2.2 | Rotating the encryption key re-encrypts stored tokens without downtime (tested with two keys). |
| AC-9.2.3 | Tokens never appear in logs, errors, API responses, activity records or anything an employee session can read (log-capture test and a tool bridge test over every registered tool). |

### 9.3 — OAuth connect flow

`POST /v1/shops/etsy/connect` (owner, MFA, recent re-authentication) returns the Etsy authorization URL with `state` and a PKCE challenge, stored server-side for 10 minutes and bound to the owner's session. Etsy redirects the browser to the web route `/integrations/etsy/callback`, which posts `code` and `state` to `POST /v1/integrations/etsy/callback` as a same-site request, so the `SameSite=Strict` session cookie is sent. The API validates `state` against that session, exchanges the code, stores tokens, creates or updates the `shops` row, grants the owner access and writes audit. Requested scopes: `shops_r`, `transactions_r`, `listings_r`; later phases add scopes by reconnecting (re-consent): Phase 10 `transactions_w` only if the probe allows, Phase 11 `listings_w`, Phase 12 the scopes publishing needs, Phase 13 `billing_r` if the ledger needs it.

| ID | Acceptance criterion |
|---|---|
| AC-9.3.1 | A callback with a missing, reused, expired or foreign `state` is rejected and audited. |
| AC-9.3.2 | Connecting the same Etsy shop twice updates one shop record instead of creating two. |
| AC-9.3.3 | Connecting does not grant shop access to any user, employee or automation other than the connecting owner and the shop's own automations. |
| AC-9.3.4 | Reconnecting with additional scopes (re-consent) updates the granted scopes on the same shop record. |

### 9.4 — Token refresh

Refresh when the access token has under five minutes left, under a per-shop advisory lock; always store the newest refresh token; warn on the shop card 10 days before the refresh token's 90-day expiry (the daily check runs on the Phase 4 scheduler; the email notification arrives with the notification slice of [Phase 10](phase-10-fulfillment-incidents.md)); mark the integration `needs_reauth` after three failed refreshes.

| ID | Acceptance criterion |
|---|---|
| AC-9.4.1 | Ten concurrent requests on an expired token cause exactly one refresh call. |
| AC-9.4.2 | A revoked refresh token leads to `needs_reauth`, shown on the shop card and an overview banner, audited, with no retry loop. |
| AC-9.4.3 | While a shop is `needs_reauth`, order intake for that shop stops calling Etsy, the freshness banner shows the age, and the other shops keep syncing. |

### 9.5 — Capability probes

At connection, daily on the Phase 4 scheduler, and on demand (a "Re-check" button): granted scopes; shop details; webhook availability (from configuration until Etsy exposes it by API); shipment write permission checked without creating a shipment (method confirmed in the Phase 0 spike; if no safe probe exists, the capability stays `unknown` and writes stay disabled); address field availability.

| ID | Acceptance criterion |
|---|---|
| AC-9.5.1 | Probe results are stored with `checked_at` and shown in the UI; a capability never shows as available without a successful probe. |
| AC-9.5.2 | The probe never performs a write that changes shop data or notifies a buyer. |

### 9.6 — Shops UI

Shop card in the web app (screen 3 of the clients specification, Shop Selector & Settings): connection status, scopes, capabilities with plain-language explanations, last successful call, token health, sync freshness; connect, reconnect and disconnect buttons with confirmation dialogs.

| ID | Acceptance criterion |
|---|---|
| AC-9.6.1 | "Capability blocked" states explain the allowed alternative (for example, "AutoVA writes tracking for you; HTCO reads it"). |
| AC-9.6.2 | All states (connected, needs re-auth, disconnected, probing, error) have screenshot tests in he/en × light/dark. |

### 9.7 — Disconnect and data protection baseline

Disconnect deletes the tokens (and revokes them at Etsy if Etsy offers a way, A-15), stops sync, marks the shop disconnected and sets a purge date 30 days ahead (the purge job runs on the Phase 4 scheduler; records under legal retention are kept). Daily backups confirmed; the restore drill of Phase 4 (mandatory test 13, first run) is repeated on the current schema just before the real shop is connected in production.

| ID | Acceptance criterion |
|---|---|
| AC-9.7.1 | After disconnect, no Etsy call is made for that shop (verified by a test with a strict HTTP mock). |
| AC-9.7.2 | A restore rehearsal record dated within 14 days, with RTO and RPO measured, exists in `docs/features/etsy-integration/operations.md` before the real shop is connected in production. |

### 9.8 — Live order intake: real webhooks and reconciliation

Switch the Phase 4 order intake to the connected shop, per shop and behind a flag:

1. **Backfill.** Import receipts of the last 30 days (or the period the owner chooses) through the reconciliation path, inside the rate-limit budget.
2. **Shadow period.** Real webhooks are verified and stored in the inbox and reconciliation runs on its schedule, but nothing downstream is triggered yet; a daily comparison checks that every receipt Etsy lists for the period exists once in `orders`, with the same items and quantities. The shadow period lasts until seven consecutive clean days (proposed; agreed with the owner).
3. **Live.** The flag is turned on; webhooks trigger Etsy Order Intake; reconciliation keeps catching anything a webhook missed; `sync_drifts` records differences (incidents from drift start in Phase 10).

Signature verification uses the real signing secret and the scheme verified at kickoff (Standard Webhooks in the Phase 4 design). If Etsy offers no webhooks for this app, the ingress stays disabled and reconciliation alone feeds the intake, as Phase 4 already supports.

| ID | Acceptance criterion |
|---|---|
| AC-9.8.1 | **Mandatory test 1 on real data.** A real signed delivery for a paid order with more than one unit (or several line items) is posted three times to the staging or production endpoint within the signature tolerance, with the owner present: one order exists, its unit count equals the sum of the receipt's quantities from the Etsy API, one `orders.ingested.v1` was emitted, and sales counters moved once. The same delivery replayed after the tolerance window is rejected and logged without its body. If webhooks are unavailable, the test runs on three reconciliation passes over the same real receipt plus the recorded fixture replay of Phase 4, and the gap is recorded in ADR-0008. |
| AC-9.8.2 | During the shadow period, every receipt Etsy lists for the period appears exactly once in `orders` with matching items and quantities; any difference blocks the switch to live. |
| AC-9.8.3 | A webhook lost on purpose (dropped at the ingress in staging) is ingested by reconciliation within one interval. |
| AC-9.8.4 | The backfill and the steady-state schedule stay inside the daily Etsy call budget (computed from the schedule in a test and checked against `x-remaining-today` in the first live week). |
| AC-9.8.5 | Turning the flag off for a shop stops webhook-triggered intake within one minute, leaves the inbox storing deliveries, and lets reconciliation catch up when it is turned back on, with no duplicate order. |

### 9.9 — Sync health on real data

The Phase 4 queue and sync health view, now fed by the real shop: webhook lag, reconciliation freshness, Etsy call budget left, drift count, dead letters. Alerts through the error-monitoring alert rules (email notifications from Phase 10).

| ID | Acceptance criterion |
|---|---|
| AC-9.9.1 | Freshness per shop is shown with its as-of time; data older than the sync window shows a stale banner on the orders screens. |
| AC-9.9.2 | An alert fires when no webhook and no reconciliation has succeeded for a shop within two intervals. |

## Test plan

Adapter contract tests against fixtures (shared with the Phase 4 fixture adapter); vault crypto tests; OAuth flow tests with a fake Etsy authorization server; concurrency test for refresh; webhook signature tests with the real scheme and test secrets; backfill and shadow comparison tests on a 200-receipt fixture; UI tests; manual verification connecting the real shop in production with the owner present; mandatory test 1 on real data (AC-9.8.1).

## Security checklist

- [ ] ASVS V10 (OAuth client): state, PKCE S256, exact redirect URI, minimal scopes.
- [ ] Tokens encrypted at rest at the application level; key rotation tested.
- [ ] Connect and disconnect require owner role, MFA and recent re-authentication; both audited.
- [ ] Webhook signature, timestamp tolerance and replay protection verified with the real secret; bodies never logged.
- [ ] No Etsy token or buyer contact field is reachable from any employee tool (tool bridge test).
- [ ] Etsy trademark notice present in the web app's footer or about page.
- [ ] Threat model updated for real buyer data entering the system.

## Documentation

`docs/features/etsy-integration/` (overview with Phase 0 findings, design, tests, security, operations including the restore rehearsal and the switch-to-live runbook); ADR-0008 validation section updated with real capabilities, webhook availability and the test 1 result; `docs/features/orders-ingest/` updated for live data; spec sheet and PLAN status.

## Demo

Connect the real shop; show the capability report; show the backfilled orders and the shadow comparison; run mandatory test 1 on a real delivery; disconnect and reconnect; show the audit trail and sync health.

## Exit gate

- [ ] All AC-9.x pass; the real shop connected in production; restore rehearsal recorded.
- [ ] Shadow period clean; live intake on; test 1 on real data recorded in ADR-0008.
- [ ] Owner has seen the demo and approved moving on.

## Rollback

Turn the live-intake flag off (reconciliation keeps the inbox and catches up later); disconnect the shop (revokes tokens); code rollback leaves encrypted tokens intact.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy denies or limits the Seller App | Personal App application in parallel (Phase 0) |
| Webhooks not available for the app | Reconciliation alone feeds the intake (Phase 4 already supports it); test 1 adapted and the gap recorded |
| No safe probe for shipment write permission | Keep the capability `unknown` and writes disabled; tracking comes from AutoVA through Etsy anyway |
| Backfill exhausts the daily call budget | Paged, throttled backfill spread over days; budget test before running |

## Cost impact

None beyond existing services. No model spend.
