# ADR-0008: Etsy integration — Seller App, webhooks plus reconciliation, read-first tracking

- **Status:** Accepted; access details to be validated in Phase 0 (spike) and Phase 4 (capability probes) with the owner's account; amended 2026-10-23 by [ADR-0022](0022-etsy-write-tools-and-browser-scope.md) (every API endpoint as a tool, writes behind approvals and the owner's first trigger)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

HTCO ingests orders from Etsy, observes tracking that AutoVA adds, and later creates and updates listings. Research on 2026-10-14 (Etsy developer docs, OpenAPI spec and official announcements) established the constraints below. Etsy's legal and help pages could not be read automatically; the owner must read the current API Terms of Use and Seller Policy.

## Facts

- **Access tiers.** A *Seller App* (launched July 2026) covers the developer's own shop, is approved automatically within minutes, and grants all public and OAuth endpoints for that shop. *Personal* and *Commercial* apps need manual review that can take weeks. Etsy has been revising its app-key process since May 2026.
- **Authentication.** OAuth 2.0 with PKCE; access tokens last 1 hour, refresh tokens 90 days; every request needs `x-api-key: <keystring>:<shared_secret>` (mandatory since February 2026).
- **Webhooks.** Available since December 2025 for orders (`order.paid`, `order.canceled`, `order.shipped`, `order.delivered`) and since October 2026 for listings and inventory. Payloads only reference the resource (`resource_url`). Signatures follow the Standard Webhooks scheme (HMAC-SHA256 over `id.timestamp.body`), with retries over about 27 hours. Registration is in the developer portal; whether Seller Apps can use webhooks must be checked.
- **Tracking writes.** `createReceiptShipment` is restricted where Etsy's Preferred Partner Program applies (United States since 2024, Israel and others since July 2026) to approved fulfillment partners, personal-use apps owned by the seller, or apps authorized before the rollout; others get 403. Every successful call emails the buyer and the endpoint has no idempotency.
- **Data limits.** No buyer messaging API; buyer email is null unless specifically granted; shipping address fields may be restricted by region; listing stats are limited to views and favourites.
- **Rate limits.** Per-second and per-day limits per API key, shown in the developer portal and in response headers; 429 with `retry-after` when exceeded.

## Decision

1. **Access.** The owner registers a **Seller App** for each shop account. No Commercial Access application unless HTCO later serves other sellers.
2. **Order ingest.** Webhooks trigger ingest when available; a **reconciliation poller always runs** (receipts modified since the last cursor minus a safety margin, every few minutes, plus a nightly 30-day sweep). If webhooks are unavailable, polling becomes the primary path with no other change.
3. **Idempotency.** Webhook events are stored in the inbox by `webhook-id`; orders are upserted by `(shop_id, receipt_id)`, items by transaction ID, shipments by shipment ID; updates apply only when Etsy's `updated_timestamp` is newer.
4. **Tracking is read-first.** We read the receipt's shipment records (written by AutoVA). Writing a shipment is a guarded, human-confirmed fallback that re-reads the receipt first and is allowed only after a capability probe succeeds.
5. **Capability probes** run at connection time and daily: granted scopes, webhook availability, shipment write permission (without sending a real shipment), address field availability. Results drive the UI.
6. **Rate-limit budget** per shop: throttle at half the per-second limit, read remaining-quota headers, back off on 429, alert when daily headroom drops.
7. **Listings.** Drafts first (`createDraftListing`), images and inventory next, activation only after approval (activation charges the listing fee).
8. **Compliance.** Required Etsy trademark notice in the apps; no buyer data in marketing lists; buyer personal data sent to AI models only if the API Terms allow it and the task needs it.
9. **OAuth callback.** The redirect URI is a web-app route, `https://dash.ht-co.app/integrations/etsy/callback`. The page posts `code` and `state` to `POST /api/v1/integrations/etsy/callback` as a same-site request, so the `SameSite=Strict` session cookie is sent and the server can bind `state` and the PKCE verifier to that session (a callback on another host, or a redirect arriving from Etsy, would not carry the cookie).
10. **Scopes grow by re-consent.** Each phase requests only what it needs; adding scopes means the owner reconnects the shop.

| Phase | Scopes | Why |
|---|---|---|
| 4–5 | `shops_r`, `transactions_r`, `listings_r` | Shop details, receipts and shipments, listings for later audit |
| 6 (only if the capability probe allows) | `transactions_w` | Guarded, human-confirmed shipment write |
| 9 | `listings_w` | Apply approved Listing Health fixes |
| 10 | `listings_w`, `listings_d` if needed | Create drafts, upload images, publish |
| 11 | `billing_r` | Payments and ledger entries for finance |

Address and email scopes are not requested unless a later feature proves it needs them.

## Consequences

- Positive: no single point of failure for ingest; no accidental duplicate shipment emails to buyers; works under the strictest access tier.
- Negative: some automation (tracking writes, buyer messaging) may stay manual depending on Etsy's rules.
- Follow-ups: Phase 0 spike with the owner's account (Seller App registration, webhook portal access, rate limits, region of the shop); Phase 4 capability probes.

## Validation

Phase 5 tests: duplicate and replayed webhooks, missed webhook recovered by reconciliation, out-of-order updates, 429 handling. Phase 6: no shipment write ever happens without a capability, a fresh re-read and a human confirmation.

## Validation log

No entries yet.
- 2026-10-23 — Amended by ADR-0022: "read-first" stays the order of delivery, but every write endpoint becomes a draft tool with an approval policy (W1–W3), write scopes requested at the owner's first write, and an owner-trigger-only start on each shop.
