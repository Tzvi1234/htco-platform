# Several Etsy shops for several people: what is allowed

| | |
|---|---|
| **Researched** | 2026-10-15, from Etsy's API Terms (2026-08-18), Seller Policy (2026-10-05), Terms of Use (2026-09-30), Help Center and developers.etsy.com; community reports are marked **UNCONFIRMED** |
| **Related** | [Etsy API capabilities](etsy-api-capabilities.md), [ADR-0008](../adr/0008-etsy-integration.md), Phases 4 and 21 |

The owner wants to manage several shops for several people from HTCO, without breaking Etsy policy and without waiting for commercial API access if he can avoid it. He asked whether a proxy per shop is the way.

## Answer in short

**No proxy.**
- **API calls** go from our server with the app key and each shop's own OAuth token, so Etsy knows exactly which shop each call is for. Rate limits are per key, not per IP. A proxy hides nothing and protects nothing.
- **Website logins:** using proxies to hide that accounts are related works against Etsy's "related accounts" rule. The API Terms also forbid "circumventing security features". Community reports say VPNs themselves can trigger suspensions (UNCONFIRMED).

## What Etsy allows

- **One person, several shops: allowed.** It requires a separate Etsy account and email per shop, and every account lists all of its owner's shops in its Public Profile. The same card, bank and tax ID may be used ("How to Open a Second Shop on Etsy").
- **The real risk:** Etsy may suspend "any accounts Etsy determines are related" when one account is suspended (Seller Policy, Terms of Use). Linking alone is not a violation; it becomes dangerous when one linked shop breaks the rules. Duplicate listings across shops are a common trigger (UNCONFIRMED).
- **Managing other people's shops: allowed through Shared Access** (September 2026). An owner invites up to 5 team members. Each signs in with their own Etsy account and 2FA.
  - Team members can handle orders, tracking and labels, listings, and buyer messages.
  - They cannot refund or cancel orders, delete listings, or see finances or settings.
  - An Etsy account belongs to one shop only, so a helper needs a separate account per shop.
  - The owner stays responsible and can download an activity log.

## The API side

- One Etsy app can be authorized by many shops through OAuth. Large tools work this way, and connecting the same legitimate app does not link shops (UNCONFIRMED, reasoned).
- **Tiers:**

  | Tier | Who can connect | Approval |
  |---|---|---|
  | **Seller App** | Your own shop only; no commercial use | Minutes |
  | **Personal App** | Limited scale: your shops plus about 4 other shops (Etsy help, localized versions) | Deeper review |
  | **Commercial Access** | Any seller who consents | Manual review after an approved Personal App. Reports say 20+ days per round, sometimes rejection or bans (UNCONFIRMED) |

- Managing several people's shops is commercial use under the API Terms.
- **Forbidden:**
  - extra keys or apps to get around tiers or limits ("strictly prohibited");
  - several apps offering the same service;
  - transferring access.
- **Paperwork for other people's shops:**
  - each seller accepts our Application Terms and privacy policy, and we act as their service provider;
  - a monitored support email;
  - the trademark disclaimer.
- **AI:** Etsy has resisted apps that rely on generative AI (a staff reply, March 2025, UNCONFIRMED), and the API Terms forbid training AI on Etsy content. Describe the AI honestly, keep humans approving content, and ask developer@etsy.com in writing if AI is central.

## How HTCO does it (decision)

1. **Ownership.** Every shop belongs to its real owner, with that owner's own account, email, identity, bank and tax details. HTCO holds no one's Etsy password.
2. **One HTCO app.**
   - It starts as a Personal App: the owner's shops plus at most about 4 others.
   - Commercial Access is requested early, before the fifth outside shop, with an honest description of the AI assistance.
   - We never create a second key or app.
3. **Connecting a shop.** The shop's owner clicks "Connect" in HTCO and approves the minimum scopes (Phase 4). Tokens are stored encrypted per shop and refreshed automatically. All calls come from our servers, throttled centrally against the one key's limits.
4. **People.** Staff work in Etsy through Shared Access with their own accounts and 2FA. In HTCO they get roles and shop grants (Phase 2 access model), and each shop's data stays isolated.
5. **Guardrails in the product.** HTCO warns before copying listings between shops. It does not scrape, does not message buyers through the API, and does not train on Etsy data. It keeps data fresh and caching minimal, and AI drafts need approval.
6. **A suspended shop is never reopened through HTCO** under another account.

## Update 2026-10-23: optional Browser Environments

The owner reports that Etsy approved browser automation of certain actions. The approval is verbal, and written confirmation is pending ([etsy-automation-approval.md](etsy-automation-approval.md)). This policy's answer stays **no proxy**: [ADR-0020](../adr/0020-per-shop-browser-environment.md) declines the requested anti-detect browser and per-shop ISP proxies, and uses an honest, stable profile per shop with one fixed egress IP for the browser workers.

Rule 1 ("HTCO holds no one's Etsy password") changes only for a shop whose real owner creates a Browser Environment and consents in the product. The password and TOTP secret are then sealed in the existing vault and released only to the browser worker ([ADR-0021](../adr/0021-authenticator-service.md)).
