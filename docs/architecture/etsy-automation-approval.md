# Etsy browser automation: the recorded approval and its scope

| | |
|---|---|
| **Approval** | **Verbal, per the owner, 2026-10-23, written confirmation pending.** Given in a conversation with people the owner knows who work at Etsy. The owner can obtain a signed confirmation. |
| **Written confirmation** | *Not attached yet.* Placeholder: attach the signed document or email as `docs/architecture/evidence/etsy-automation-approval-<date>.pdf` (link it here), with the name and role of the person at Etsy who signed it. |
| **Scope recorded by** | The owner, in the table below. Engineering never fills in a scope. |
| **Related** | [ADR-0022](../adr/0022-etsy-write-tools-and-browser-scope.md) (what this page controls), [ADR-0020](../adr/0020-per-shop-browser-environment.md) (the honest browser environment), [multi-shop policy](etsy-multi-shop-policy.md), [Phase 8A](../plan/phases/phase-08a-browser-infrastructure.md) |

## How this page is enforced

- Every browser action on etsy.com has a row below and a feature flag `browser.etsy.<action_id>`.
- A flag can be switched on only when its row has the **Approved scope** column filled in by the owner, and a **Scope version** (the date of the row's last change).
- The Execution Router checks the flag and the scope version at execution time. A row whose scope is empty, removed or "No" keeps the flag **off**. Changing a row's scope invalidates the flag until it is switched on again.
- Default for every row: **flag off.**
- Any Etsy warning, security-challenge spike (ADR-0020 rule 5) or restriction switches every `browser.etsy.*` flag off at once (the global browser kill switch), and the affected work moves to a person.

## Risk the owner accepts by recording a scope

A verbal approval from Etsy employees may not bind Etsy's **Trust & Safety** team, and it does not change Etsy's Terms of Use or API Terms, which restrict automated access to etsy.com. If Etsy enforces against an action, the approval may not protect the shop, or the shops related to it. Recommendations:

1. Obtain the written confirmation **before switching on any write action** (B-05 to B-10).
2. In the confirmation, name the actions, the shops, and the fact that several managed shops' sessions come from one HTCO egress IP (ADR-0020, decision D-17).
3. Start with the read-only exports (B-02, B-03, B-04).

## Scope table (for the owner to fill in)

"Approved scope" is in the owner's words: what Etsy agreed to, for which shops, any limits on volume or frequency, and the date.

| ID | Browser action on etsy.com | Why (the API cannot) | Kind | Approved scope (owner fills in) | Scope version | Flag | Approval policy in HTCO | Phase |
|---|---|---|---|---|---|---|---|---|
| B-01 | Sign in to the shop's account and keep the session (password and TOTP from the vault; challenges to a person) | Needed by every other row | Session | — | — | off | Owner creates the environment with step-up | 8A |
| B-02 | Download the Shop Manager **stats** export (visits, views, conversion, traffic sources, search terms) | No stats API | Read (export) | — | — | off | None per run; audited | 8A |
| B-03 | Download the **Etsy Ads** report export | No ads API | Read (export) | — | — | off | None per run; audited | 8A |
| B-04 | Read the **Messages** inbox and conversation text for matching with orders | No messages API | Read | — | — | off | None per run; buyer content DC2, quarantined | 14 |
| B-05 | Send an **approved reply** in a buyer conversation | No messages API | Write (contacts a buyer) | — | — | off | W2: approval of the exact text | 14 |
| B-06 | Read **cases** and help requests on orders | No cases API | Read | — | — | off | None per run; audited | 14 |
| B-07 | Post an **approved reply to a review** | No review-reply API | Write (public) | — | — | off | W2 | 14 |
| B-08 | Create or end a **coupon or sale** with approved terms | No coupons API | Write (money) | — | — | off | W3 (Owner) | 13 |
| B-09 | Issue an approved **refund** or **cancel** an order | No refund API | Write (money) | — | — | off | W3 (Owner) | 13 |
| B-10 | Switch **Etsy Ads** on or off per listing, or change the daily budget, with approved values | No ads API | Write (money) | — | — | off | W3 (Owner) | 13 |
| B-11 | Download the **monthly statement** or the payment account CSV | Statements are not in the API (the ledger is) | Read (export) | — | — | off | None per run; audited | 13 |

Not candidates, never automated: buying shipping labels, opening new shops or accounts, Shared Access invitations, anything in another person's account, and anything that solves or avoids a security check.

## Change log

| Date | Change | By |
|---|---|---|
| 2026-10-23 | Page created; approval recorded as verbal; every row empty and every flag off | Engineering, on the owner's instruction |
