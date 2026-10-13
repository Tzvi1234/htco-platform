# Assumptions and Open Decisions

| | |
|---|---|
| **Purpose** | Every unverified assumption and every pending decision, with an owner and a deadline. Nothing here may silently become "fact" in code. |
| **Last updated** | 2026-10-14 |

Status: **Open** (not yet checked) · **Verified** · **Rejected** (proved wrong; plan adjusted) · **Decided**.

## 1. Owner decisions needed

| ID | Decision | Recommendation | Needed by | Status |
|---|---|---|---|---|
| D-01 | Upgrade GitHub to Pro (about $4/month) for branch rulesets and required checks on private repositories | Yes | Phase 1 | Open |
| D-02 | Region for DigitalOcean and Supabase | Frankfurt (`fra` + `eu-central-1`) | Phase 2 | Open |
| D-03 | Start the first AI workflow with Listing Health instead of Product Studio | Yes ([MVP.md](MVP.md)) | Phase 8 | Open |
| D-04 | Catalog model on Etsy, given Etsy's reselling and production-partner rules | Owner review with Etsy's current Seller Policy before Phase 10 | Phase 10 | Open |
| D-05 | Native app order: Android after the operational core, Windows last | Yes | Phase 13 | Open |
| D-06 | Monthly AI budget cap for the pilot | Owner sets a number | Phase 8 | Open |
| D-07 | Repository license while private | Proprietary, all rights reserved; revisit before going public | Now | Decided (proprietary) |
| D-08 | A machine account for AI agents' pull requests, so the owner can review and approve them | Yes, from Phase 1 | Phase 1 | Open |
| D-09 | Point-in-time recovery (about $100+/month) vs daily backups | Daily backups for the pilot; decide before launch | Phase 16 | Open |
| D-10 | Hermes integration as an Owner Assistant channel | Decide in Phase 13 | Phase 13 | Open |
| D-11 | DNS provider for `ht-co.app` | Same provider as the owner's other domains, or Cloudflare | Phase 1 | Open |
| D-12 | Git commit identity for these repositories (current global email or the GitHub no-reply address) before they ever become public | GitHub no-reply address | Before going public | Open |

## 2. Assumptions to verify

| ID | Assumption | How to verify | Owner | By | Status |
|---|---|---|---|---|---|
| A-01 | The domain `ht-co.app` is available or owned | Registrar search | Owner | Phase 0 | Open |
| A-02 | The owner can register an Etsy Seller App for the pilot shop | Etsy developer portal | Owner | Phase 0 | Open |
| A-03 | Etsy order webhooks are available to a Seller App | Developer portal "webhooks" option | Owner + platform | Phase 0 | Open |
| A-04 | `createReceiptShipment` is permitted for our app in the shop's region | Capability probe on a real receipt without sending a shipment | Platform | Phase 4 | Open |
| A-05 | AutoVA writes tracking numbers to Etsy for every fulfilled order | Compare 20 recent orders in Etsy and AutoVA | Owner | Phase 0 | Open |
| A-06 | AutoVA offers an API, export or partner integration | Ask AutoVA support | Owner | Phase 0 | Open |
| A-07 | AutoVA's automatic supplier ordering is active for the shop (their site calls it a concept demonstration) | Owner's AutoVA account | Owner | Phase 0 | Open |
| A-08 | Etsy rate limits for our key are enough for polling every few minutes | Developer portal numbers | Owner | Phase 0 | Open |
| A-09 | Etsy's current API Terms allow storing order data for our retention periods and sending buyer data to AI models when needed | Owner reads the live terms | Owner | Phase 4 | Open |
| A-10 | LangGraph works on Python 3.14 in our setup | Smoke test in Phase 1 CI | Platform | Phase 1 | Open |
| A-11 | The Python Supabase client supports a server-side PKCE exchange (or the Auth REST API is used directly) | Spike | Platform | Phase 2 | Open |
| A-12 | Server-sent events survive DigitalOcean's request timeout with reconnects, or Supabase Realtime is needed | Spike | Platform | Phase 13 | Open |
| A-13 | Supabase Micro compute (60 direct connections) is enough for API, worker and agent pools | Load test | Platform | Phase 8 | Open |
| A-14 | The shop's buyers are mostly in the US (affects region and data-protection scope) | Order data after Phase 5 | Platform | Phase 5 | Open |
| A-15 | Etsy offers a way to revoke an app's OAuth tokens programmatically (otherwise the owner removes access in Etsy's settings) | Etsy docs and a test during the Phase 4 spike | Platform | Phase 4 | Open |
| A-16 | Supabase Auth's OAuth 2.1 server can back a system-browser sign-in for the native apps with an HTCO-hosted sign-in page | Spike at the start of Phase 14 | Platform | Phase 14 | Open |

## 3. Verified facts (research on 2026-10-14)

| ID | Fact | Source |
|---|---|---|
| V-01 | DigitalOcean App Platform cannot connect to IPv6 hosts; Supabase direct connections need the IPv4 add-on | DigitalOcean limits page; Supabase connection docs |
| V-02 | Supabase Queues (pgmq) is public alpha with no built-in dead-letter queue | Supabase Queues page |
| V-03 | Etsy offers order webhooks (since 2025-12) and listing and inventory webhooks (since 2026-10) | Etsy developer docs and announcements |
| V-04 | Etsy restricts shipment writes for new apps in several regions, including the US and Israel | Etsy OpenAPI spec and announcements |
| V-05 | Etsy has no buyer messaging API | Etsy OpenAPI spec |
| V-06 | GitHub branch rulesets on private repositories need GitHub Pro or higher; environment approval rules are not available for private repositories on Free, Pro or Team | GitHub plan docs; confirmed by a 403 on the owner's account |
| V-07 | The "Jev" site in the original brief (jevtypesafeai.com) states in its terms that it is not affiliated with TypeSafe AI; Jev itself comes from TypeSafe AI and is in early access | [jevtypesafeai.com/terms](https://jevtypesafeai.com/terms), [typesafe.ai](https://typesafe.ai) |
| V-08 | AutoVA has no public API or developer docs | autova.io |
