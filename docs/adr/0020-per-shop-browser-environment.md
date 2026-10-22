# ADR-0020: An optional, honest per-shop Browser Environment: no anti-detect browser, no proxy per shop

- **Status:** Accepted (owner's decision, 2026-10-23: "the recommended honest design")
- **Date:** 2026-10-23
- **Deciders:** Owner, engineering
- **Related:** [ADR-0019](0019-unified-execution-engine-and-action-gateway.md) (the Browser channel), [ADR-0021](0021-authenticator-service.md) (TOTP), [ADR-0022](0022-etsy-write-tools-and-browser-scope.md) (what a browser may do on Etsy), [Etsy multi-shop policy](../architecture/etsy-multi-shop-policy.md), [Phase 8A](../plan/phases/phase-08a-browser-infrastructure.md)

## Context

The owner's brief of 2026-10-23 adds an optional **Browser Environment** per shop. It is used for work that Etsy's API cannot do, within the approval the owner reports from Etsy ([etsy-automation-approval.md](../architecture/etsy-automation-approval.md)).

- Adding a shop stays OAuth-only.
- "Create Browser Environment" is offered at any time afterwards. It provides:
  - a persistent browser profile (cookies, storage, sessions);
  - encrypted credentials and a TOTP authenticator;
  - permissions for agents and automations;
  - login, environment and health status, with logs and history.
- Shops are isolated from one another.

### What the owner's brief asked for

The brief specified the environment as:

- **AdsPower Professional** with its **SunBrowser** anti-detect fingerprinting, driven through the **AdsPower Local API**;
- **a dedicated static ISP proxy per shop**, so that every shop appears on its own residential IP.

### Why that design is not adopted

These are anti-detection and account-linkage evasion tools. HTCO's own [multi-shop policy](../architecture/etsy-multi-shop-policy.md) already records the governing facts:

- Using proxies to hide that accounts are related works against Etsy's **related-accounts** rule (Seller Policy and Terms of Use: Etsy may suspend "any accounts Etsy determines are related").
- Etsy's API Terms forbid **"circumventing security features"**. Fingerprint spoofing and per-shop residential IPs exist precisely to defeat Etsy's account-security and linkage signals.
- Community reports say VPNs and proxies themselves can trigger suspensions (UNCONFIRMED). A hidden link that Etsy later finds is worse than an honest one: one suspended shop can take the related shops with it.
- IP separation does not hide linkage in any case. The shops share one HTCO app, one OAuth client, payment details and an owner (Etsy's own "second shop" guidance), so Etsy can link them anyway. The brief itself says: "Never assume IP separation hides shop linkage from Etsy."

On 2026-10-23 the owner chose the honest design below, after reading these reasons.

## Decision drivers

- Stable, reliable sessions: fewer logins, fewer challenges.
- Nothing that misrepresents who is acting or from where.
- Isolation between shops inside HTCO: separate data, credentials, locks and permissions.
- Free and open-source components. No vendor whose product is evasion.

## Options considered

1. **AdsPower Professional + SunBrowser + a static ISP proxy per shop** (the brief). Rejected for the reasons above. It would also cost money: AdsPower's paid tiers plus roughly one proxy per shop per month.
2. **Hosted browser services** (Browserbase, Steel and similar). Honest, but they are paid, they put the shops' sessions on a third party, and several offer "stealth" modes that we would have to keep switched off.
3. **Self-hosted Chromium per shop, with honest defaults.** Either a Playwright persistent context (`launch_persistent_context` with a per-shop `user_data_dir`) or a CDP-controlled Chromium, run in HTCO's browser workers. **Adopted.**

## Decision

1. **An optional environment per shop.** `shop_browser_environments` holds one row per shop, with the states `NOT_CONFIGURED`, `PROVISIONING`, `READY`, `LOGIN_REQUIRED`, `DEGRADED`, `SUSPENDED` and `FAILED` ([state machine](../architecture/execution-engine.md#7-state-machines)). Creating one is an explicit, audited Owner or Admin action that needs step-up re-authentication.
2. **The profile.**
   - Each shop gets one Chromium `user_data_dir`, encrypted at rest. It is stored as a sealed archive in Supabase Storage under the existing vault's envelope keys, and unpacked only inside a browser worker for the lifetime of a session.
   - The browser keeps its stock fingerprint: real user agent, real time zone, no spoofing plug-ins, no "stealth" patches. It is honest by default: we do not lie about the browser, the machine or the location.
   - Shops never share a profile, cookies, storage or a credential.
3. **One engine per profile at a time.** The Browser Controller holds a Postgres advisory lock per profile (`pg_try_advisory_lock` on the profile ID) for the whole session. A second request waits or is refused. It never opens the same profile twice.
4. **Network egress.**
   - The browser workers use **one fixed, truthful egress IP**, the platform's own. It is consistent across shops and sessions, and it is never rotated to look like different people.
   - The API workers may use a **separate** egress for network isolation (blast radius, allowlists), never for disguise.
   - There is no residential, ISP or rotating proxy, and no VPN.
   - Preflight checks verify that the egress IP is the expected one, so a misconfiguration is caught, not exploited.
5. **Security challenges go to a human.**
   - A CAPTCHA, a WebAuthn or passkey prompt, an "unusual activity" page, a block, or a permission error stops the session. The environment moves to `LOGIN_REQUIRED` or `DEGRADED`, and a Human-channel task opens.
   - Challenges are never solved automatically and never retried in a loop. After two consecutive challenges in 24 hours, the environment is `SUSPENDED` until a person reviews it.
6. **Credentials and TOTP.**
   - The shop's Etsy password and TOTP secret live in the existing vault, never in the profile or a prompt. They are released to the browser worker through the Secret Broker ([ADR-0021](0021-authenticator-service.md)).
   - Storing a shop's Etsy password at all is a change from the multi-shop policy's "HTCO holds no one's Etsy password". It applies only to shops whose real owner creates a Browser Environment and consents in writing, in the product.
7. **Pace and scope.**
   - Human pace: per-shop concurrency of one, and per-action rate limits.
   - Only actions inside the recorded Etsy approval scope run, each behind its own feature flag ([ADR-0022](0022-etsy-write-tools-and-browser-scope.md)).
8. **Separation of duties.** API workers, browser workers, profiles, network egress and secrets are separate: different processes, database roles, storage prefixes and network paths. Nothing here weakens the existing encryption, permissions or RLS.

## Consequences

- Positive:
  - nothing to hide if Etsy asks how HTCO works;
  - no per-shop proxy bill;
  - stable sessions from persistent profiles;
  - one design that also serves non-Etsy sites (POD partners, suppliers).
- Negative:
  - Etsy sees several shops' sessions from one IP. This is true, and it is consistent with the linkage Etsy can already see through the shared app and owner;
  - challenges cost a person's time. Their rate is a tracked metric (Phase 8A).
- Open question for the owner, in [ASSUMPTIONS D-17](../product/ASSUMPTIONS.md#1-owner-decisions-needed):
  - whether to ask Etsy, in the same written confirmation as the automation scope, that several managed shops' browser sessions from one HTCO egress IP are acceptable;
  - recommended: yes.

## Validation

- Phase 8A tests prove it:
  - two shops' sessions never share a profile or a lock;
  - preflight refuses an unexpected egress IP;
  - a simulated CAPTCHA page stops the session and opens one human task, with no retry.
- Weekly metrics: challenges per shop, and sessions that ended in `LOGIN_REQUIRED`.

## Validation log

- 2026-10-23 — The owner chose the honest design (stable per-shop profile, one fixed egress IP for the browser workers, no anti-detect browser, no proxy per shop).
