# Phase 0 — Groundwork and decisions

| | |
|---|---|
| **Milestone** | M0 — Plan approved |
| **Repositories** | both |
| **Size** | S |
| **Depends on** | — |
| **Requirements** | FR-SUP-04 |
| **Status** | In progress |

## Progress

- [x] 0.1 Repositories and documentation baseline
- [x] 0.2 Design system v1
- [ ] 0.3 Etsy access spike (read-only, owner's account)
- [ ] 0.4 AutoVA fact check
- [ ] 0.5 Decisions closed

## Goal

Turn the original planning brief into an approved, verified starting point: repositories, documentation, design system, decisions, accounts, and facts about Etsy and AutoVA checked against reality, so that Phase 1 starts with no open blockers.

## Why now

Every later phase depends on decisions made here (region, access tiers, catalog model) and on accounts that take time to approve (Etsy app, code-signing identity later). Finding out in Phase 4 that webhooks are unavailable, or in Phase 10 that the catalog model conflicts with Etsy policy, would be expensive.

## Scope

**In:** private repositories with clean structure and docs; PRD, MVP, plan, software design, engineering rules, testing strategy, skills, ADRs; design system v1 (tokens and component guidelines); account setup; verification spikes that only read data.

**Out:** application code, cloud resources that cost money before approval, any write to Etsy or AutoVA.

## Owner prerequisites

| # | Task | Why |
|---|---|---|
| O-1 | Review and approve PRD, MVP, PLAN, SOFTWARE_DESIGN, ENGINEERING_RULES and the design system | They govern every phase |
| O-2 | Upgrade GitHub to Pro; decide on a machine account for agent pull requests (D-01, D-08) | Rulesets and required checks on private repos |
| O-3 | Choose the region (D-02) | Latency and data location |
| O-4 | Buy or confirm `ht-co.app`; choose DNS provider (A-01, D-11) | Domains for app, API and mail |
| O-5 | Create the Supabase organization and DigitalOcean team with 2FA and billing alerts (DigitalOcean has no hard spending cap: a monthly spend alert plus fixed-size resources without autoscaling) | No resources yet; ready for Phase 1–2 |
| O-6 | Register an Etsy Seller App for the pilot shop; note rate limits and whether a webhook portal appears (A-02, A-03, A-20) | Phase 4–5 design |
| O-7 | Ask AutoVA about an API, export or partner integration; confirm whether automatic ordering is active and whether tracking reaches Etsy for every order (A-05, A-06, A-07) | Phases 6 and 13 |
| O-8 | Read Etsy's current API Terms and Seller Policy (reselling, production partners, AI disclosure, photos) (A-21, D-04) | Product Studio and data retention |

## Slices

### 0.1 — Repositories and documentation baseline

- Private repositories `htco-platform` and `htco-clients` with README, CLAUDE.md, CONTRIBUTING, SECURITY, LICENSE (proprietary), CODEOWNERS, PR and issue templates.
- Product, plan, architecture, engineering and ADR documents; feature documentation template; skills for software engineering, TDD, feature verification and feature docs.
- **Acceptance:** AC-0.1.1 every document is linked from a docs index; AC-0.1.2 no file in either repository is a scratch note, session log, empty placeholder or duplicate; AC-0.1.3 links resolve.

### 0.2 — Design system v1

- Tokens in DTCG format derived from the official logo's measured colours (navy `#030F1F`, turquoise `#05F7F4`, amber `#F8B857`), light and dark themes with WCAG-checked contrast, type, spacing, radius, elevation and motion; component inventory and guidelines; a browsable design system reference.
- **Acceptance:** AC-0.2.1 `node design/tokens/check-contrast.mjs` passes: every text token meets 4.5:1 and every control boundary, focus ring, icon and status dot meets 3:1 on every background it may appear on, in both themes (202 pairs); AC-0.2.2 the official logo file is used unmodified.

### 0.3 — Etsy access spike (read-only, owner's account)

- With the Seller App's key: authorize the pilot shop with PKCE from a local script, read the shop, list the last 20 receipts with shipments, read the rate-limit headers, list listings with inventory. Check the webhook portal.
- **Acceptance:** AC-0.3.1 a short report in `docs/features/etsy-integration/overview.md` records scopes granted, rate limits, webhook availability, shop region and whether receipts carry AutoVA's tracking; AC-0.3.2 no write call is made; AC-0.3.3 the script and its tokens never enter the repository.

### 0.4 — AutoVA fact check

- Owner provides AutoVA's answers (O-7); compare 20 recent orders: tracking present in Etsy, timing from payment to tracking.
- **Acceptance:** AC-0.4.1 ADR-0009 and ASSUMPTIONS updated with the answers; AC-0.4.2 a first SLA window proposal per shipping method (median and 90th percentile time to tracking).

### 0.5 — Decisions closed

- D-01, D-02, D-03, D-05, D-07, D-08, D-11 decided and recorded; ADR-0005 accepted with the chosen region.
- **Acceptance:** AC-0.5.1 no "Open" decision in ASSUMPTIONS is needed by Phase 1 or 2.

## Test plan

Documentation checks: every relative link in both repositories resolves, and the token contrast check (`htco-clients/design/tokens/check-contrast.mjs`) passes all 202 pairs (run on 2026-10-14). Phase 3 runs it in CI.

## Security checklist

- [ ] No secrets, tokens or personal data in any committed file (scan both repositories).
- [ ] Etsy spike tokens stored only on the owner's machine and revoked after the spike if not reused.
- [ ] GitHub, Supabase and DigitalOcean accounts have 2FA enabled.

## Documentation

All documents created in slice 0.1; this phase's results recorded in ASSUMPTIONS, ADR-0005, ADR-0008 and ADR-0009.

## Demo

Walk the owner through the plan, the PRD, the MVP, the design system and the open decisions; agree on the answers.

## Exit gate

- [ ] Owner approved the documents (O-1).
- [ ] Prerequisites O-2 to O-6 done; O-7 and O-8 at least in progress.
- [ ] Etsy spike report merged.
- [ ] Phase 1 has no open blocker.

## Rollback

Nothing deployed. Documents are versioned in git.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy Seller App unavailable or slow for the shop | Fall back to a Personal App application; plan Phase 4 to use whichever key exists |
| Catalog model conflicts with Etsy policy | Decision D-04 before Phase 12; Phases 1–11 do not depend on it |

## Cost impact

GitHub Pro about $4/month; domain registration (yearly). No hosting cost yet.
