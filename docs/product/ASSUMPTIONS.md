# Assumptions and Open Decisions

| | |
|---|---|
| **Purpose** | Every unverified assumption and every pending decision, with an owner and a deadline. Nothing here may silently become "fact" in code. |
| **Last updated** | 2026-10-23 |

Status: **Open** (not yet checked) · **Verified** · **Rejected** (proved wrong; plan adjusted) · **Decided**.

## 1. Owner decisions needed

| ID | Decision | Recommendation | Needed by | Status |
|---|---|---|---|---|
| D-01 | Upgrade GitHub to Pro (about $4/month) for branch rulesets and required checks on private repositories | Yes | Phase 1 | Decided 2026-10-14: Pro active; `main` rulesets on both repositories |
| D-02 | Region for DigitalOcean and Supabase | Frankfurt (`fra` + `eu-central-1`) | Phase 2 | Decided 2026-10-14: Frankfurt; container registry GHCR |
| D-03 | Start the first content workflow with Listing Health instead of Product Studio | Yes ([MVP.md](MVP.md)) | Phase 11 | Open |
| D-04 | Catalog model on Etsy, given Etsy's reselling and production-partner rules | Owner review with Etsy's current Seller Policy before Phase 12 | Phase 12 | Open |
| D-05 | Native app order: Android after the operational core, Windows last | Yes | Phase 15 | Open |
| D-06 | Monthly AI budget cap for the pilot | Owner sets a number | Phase 9 | Open |
| D-07 | Repository license while private | Proprietary, all rights reserved; revisit before going public | Now | Decided (proprietary) |
| D-08 | A machine account for AI agents' pull requests, so the owner can review and approve them | Yes, from Phase 1 | Phase 1 | Open |
| D-09 | Point-in-time recovery (about $100+/month) vs daily backups | Daily backups for the pilot; decide before launch | Phase 20 | Open |
| D-10 | Hermes integration as a channel for Lia (Owner Assistant) | Decide in Phase 15 | Phase 15 | Open |
| D-11 | DNS provider for `ht-co.app` | Same provider as the owner's other domains, or Cloudflare | Phase 1 | Open |
| D-12 | Git commit identity for these repositories (current global email or the GitHub no-reply address) before they ever become public | GitHub no-reply address | Before going public | Open |
| D-13 | The living-office organization (16 employees, Jev, automations, tools, engineering) | Accepted as [ADR-0012](../adr/0012-living-office-organization.md) | Now | Decided (owner's brief, 2026-10-14) |
| D-14 | Autonomy level of each employee at activation | Start at the level in PRD §8.1; promote only after its evaluation | Each activation phase | Open |
| D-15 | Whether engineering employees (Alex, Amir, Iris) may open pull requests under a machine account | Yes, with D-08; merging and releasing stay with the owner | Phase 19 | Open |
| D-16 | Etsy's approval of browser automation: (a) keep it verbal and enable read-only exports only; (b) obtain the signed confirmation first and then enable rows as scoped; (c) do not use browser automation on Etsy | (b) before any write row; (a) is acceptable for B-02 and B-03 ([approval page](../architecture/etsy-automation-approval.md)) | Phase 8A | Decided 2026-10-23: every browser action on Etsy, read or write, needs the owner's approval each time it runs; automatic (unattended) actions are defined later, per action, once the flows are final. The written confirmation is still pending |
| D-17 | Ask Etsy, in the same confirmation, that several managed shops' browser sessions come from one HTCO egress IP: (a) yes; (b) no | (a) | Phase 8A | Open |
| D-18 | Fixed egress for browser workers: (a) a basic droplet with its own static IP ($12, recommended); (b) App Platform with a dedicated egress IP (up to $25 more); (c) the same egress as the API | (a) | Phase 8A | Decided 2026-10-23: (c) for now, the same egress as the API, at no extra cost. The owner wants to revisit buying IPs later; a per-shop IP must never be used to hide shop linkage (ADR-0020). 2026-10-26: [ADR-0025](../adr/0025-browser-and-network-providers.md) §4 adds an optional dedicated egress per shop: an HTCO-owned static IP, allocated by the Owner, at about $5 a month per shop when enabled ([COST_MODEL](../cost/COST_MODEL.md)). 2026-10-26, later: **(a) for the browser worker.** Its egress rules for Chromium's OS user need `NET_ADMIN`, which App Platform does not grant, so the browser worker runs on a basic HTCO droplet (about $12 a month for 2 GB), and the shared browser egress becomes that droplet's own IP. The owner was told and agreed. The API keeps its own egress |
| D-19 | Order store naming: (a) keep `etsy_orders` (#57) as the store and add a channel-neutral `orders` view at Phase 21; (b) rename now to `orders` with a `marketplace` column | (a), so merged #57 is untouched ([Phase 5](../plan/phases/phase-05-orders-ingest.md#merged-57--order-history-store)) | Phase 5 | Open |
| D-20 | The owner's request for AdsPower and per-shop ISP proxies: (a) accept the honest design of ADR-0020; (b) revisit after Etsy's written answer to D-17 | (a) | Now | Decided 2026-10-23: (a). 2026-10-26: amended by [ADR-0025](../adr/0025-browser-and-network-providers.md): a layered `BrowserProvider` contract with `ManagedChromiumProvider` as its only implementation, honest configuration profiles, and an optional HTCO-owned egress per shop. AdsPower is recorded as the owner's chosen external provider. It is out of our scope and not enabled. Any future provider must implement the contract and pass the same gates. An anti-detect browser fails ADR-0025 §2's honesty checks by design, so it can never be enabled under that ADR. Per-shop ISP, residential or rotating proxies stay out |
| D-21 | Which Etsy write the owner tries first: (a) as planned, the shipment upload in Phase 6 (slice 6.8, where A-04 allows it), then the reversible shop writes in Phase 7 (7.12); (b) move the reversible shop writes (vacation mode, a section) forward into Phase 6, so the first write is easy to undo | (b) if A-04 shows shipments are blocked for our key; otherwise (a) | Phase 6 | Open |
| D-23 | The AutoVA quote mailbox: (a) a forwarding rule from the owner's mailbox that sends only AutoVA mail to `quotes@office.ht-co.app` (Resend inbound); (b) read-only Gmail OAuth filtered to AutoVA's sender | (a): HTCO sees only AutoVA mail and needs no token for the whole mailbox ([AutoVA integration](../architecture/autova-integration.md#the-quote-mailbox-d-23)) | Phase 7 | Open |
| D-24 | How supplier orders are paid | AutoVA's own Auto Pay, switched on by the owner in AutoVA; **HTCO never pays and never clicks Pay**; an Unpaid order only raises an alert and a task | Now | Decided 2026-10-23 (owner) |
| D-22 | `agent-worker` deployment (PR #54, +$5/month) | Approve before slice 5.11 | Phase 5 | Decided 2026-10-23: approved, fixed-size instance with no autoscaling; the deploy budget check caps the worker at $20 a month (DigitalOcean has no hard spend cap) |

## 2. Assumptions to verify

| ID | Assumption | How to verify | Owner | By | Status |
|---|---|---|---|---|---|
| A-01 | The domain `ht-co.app` is available or owned | Registrar search | Owner | Phase 0 | Open |
| A-02 | The owner can register an Etsy Seller App for the pilot shop | Etsy developer portal | Owner | Phase 0 | Open |
| A-03 | Etsy order webhooks are available to a Seller App | Developer portal "webhooks" option | Owner + platform | Phase 0 | Open |
| A-04 | `createReceiptShipment` is permitted for our app in the shop's region | Capability probe on a real receipt without sending a shipment | Platform | Phase 4 | Open |
| A-05 | AutoVA writes tracking numbers to Etsy for every fulfilled order | Compare 20 recent orders in Etsy and AutoVA | Owner | Phase 0 | Answered 2026-10-23: no; the owner copies tracking from AutoVA to Etsy by hand. Covered by the relay (slice 6.10) |
| A-06 | AutoVA offers an API, export or partner integration | Ask AutoVA support | Owner | Phase 0 | Open |
| A-07 | AutoVA's automatic supplier ordering is active for the shop (their site calls it a concept demonstration) | Owner's AutoVA account | Owner | Phase 0 | Answered 2026-10-23: yes. AutoVA creates the supplier order for a mapped product and pays with its Auto Pay; for a new product it requests quotes by email ([AutoVA integration](../architecture/autova-integration.md)) |
| A-08 | Where AutoVA shows tracking, how its account signs in (password, TOTP or another provider), and whether its terms allow reading the owner's own account with a browser | The owner checks his AutoVA account and AutoVA's terms | Owner | Phase 6 | Partly answered 2026-10-23: tracking is on the Orders page (with carrier, status, paid flag and profit) and in its Export file; sign-in is email and password with no TOTP. **Open:** whether AutoVA's terms allow reading the account (A-01) and approving quotes (A-02) with a browser |
| A-09 | AutoVA's quote emails: the sending address, the layout, and whether tracking emails also exist | The owner forwards three sample quote mails and names the sender | Owner | Phase 7 | Changed 2026-10-23: every new-product quote arrives by email (slice 7.14); the sending address is not known yet. Tracking comes from AutoVA's Export and Orders page (A-01), not email |
| A-20 | Etsy rate limits for our key are enough for polling every few minutes (numbered A-08 until 2026-10-23) | Developer portal numbers | Owner | Phase 0 | Open |
| A-21 | Etsy's current API Terms allow storing order data for our retention periods and sending buyer data to AI models when needed (numbered A-09 until 2026-10-23; ADR-0018's "assumption A-09" means this row) | Owner reads the live terms | Owner | Phase 4 | Open |
| A-10 | LangGraph works on Python 3.14 in our setup | Smoke test in Phase 1 CI | Platform | Phase 1 | Verified 2026-10-14 (langgraph 1.2.14 on CPython 3.14.8, `tests/agent_runtime`) |
| A-11 | The Python Supabase client supports a server-side PKCE exchange (or the Auth REST API is used directly) | Spike | Platform | Phase 2 | Open |
| A-12 | Server-sent events survive DigitalOcean's request timeout with reconnects, or Supabase Realtime is needed | Spike | Platform | Phase 15 | Open |
| A-13 | Supabase Micro compute (60 direct connections) is enough for API, worker and agent pools | Load test | Platform | Phase 8 | Open |
| A-14 | The shop's buyers are mostly in the US (affects region and data-protection scope) | Order data after Phase 5 | Platform | Phase 5 | Open |
| A-15 | Etsy offers a way to revoke an app's OAuth tokens programmatically (otherwise the owner removes access in Etsy's settings) | Etsy docs and a test during the Phase 4 spike | Platform | Phase 4 | Rejected 2026-10-16: no revocation endpoint in Etsy's authentication guide or OpenAPI spec; disconnect deletes HTCO's tokens and the owner can remove access at Etsy ([operations](../features/etsy-integration/operations.md#disconnecting-a-shop)) |
| A-16 | Supabase Auth's OAuth 2.1 server can back a system-browser sign-in for the native apps with an HTCO-hosted sign-in page | Spike at the start of Phase 16 | Platform | Phase 16 | Open |
| A-17 | Jev's API, terms, limits and price are as reported (decision-only model; early access via TypeSafe and OpenRouter; about $0.042 per million input tokens, output free; 32K or 64K context) | TypeSafe documentation and a sandbox call | Platform | Phase 9 | Open |
| A-18 | Jev is at least as accurate as rules plus an economy model on our triage questions | 200-example benchmark per task family (FR-MDL-08) | Platform | Phase 9 | Open |
| A-19 | One `agent-worker` carries automations and employees at pilot load | Load test and queue-age metrics | Platform | Phase 10 | Open |

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
| V-09 | Jev is TypeSafe AI's decision-only model (typed choice and score answers, no free text), launched in September 2026 and listed on OpenRouter | [OpenRouter: Jev 1.13](https://openrouter.ai/typesafe/jev-1.13), [OpenRouter: What is Jev](https://openrouter.ai/blog/insights/what-is-jev/) |
