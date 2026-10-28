# ADR-0009: AutoVA behind a port, tracking read from Etsy

- **Status:** Accepted; amended 2026-10-28 (AutoVA does not write tracking to Etsy, A-05)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The original planning brief assumes AutoVA places supplier orders automatically once a supplier offer is approved per product or variant, and that HTCO reads supplier quotes and tracking from it. A review of autova.io on 2026-10-14 found:

- AutoVA describes itself as an analytics and operations workspace for Etsy sellers: profit analytics, an AI listing studio, supplier sourcing, an order inbox, team roles. Etsy is the only working marketplace connection; others are "in progress".
- Its site says it writes tracking numbers to Etsy through the Etsy API when an order is marked shipped, and syncs orders every 15 minutes. **The owner answered on 2026-10-23 that it does not do this for his shop** ([ASSUMPTIONS](../product/ASSUMPTIONS.md) A-05): he copies each tracking number from AutoVA to Etsy by hand.
- Its own FAQ calls the automatic-ordering demonstration on its homepage a "concept demonstration" and asks users to confirm availability in their account.
- There is **no public API, webhook or developer documentation**.

## Decision

1. All AutoVA interaction goes through an `AutoVaPort` with operations for quotes, supplier mappings and order or tracking status.
2. **HTCO reads tracking from Etsy** (ADR-0008), with no AutoVA credentials. AutoVA does **not** write tracking to Etsy (A-05, amended 2026-10-28): the owner copies it from AutoVA into Etsy. Until a tracking number reaches Etsy, the AutoVA tracking relay ([Phase 6](../plan/phases/phase-06-fulfillment-incidents.md), slice 6.10) opens one relay item per order past its window (A-20), where a person pastes the number and copies it into Etsy; the upload through `etsy.shipments.create` (6.6, 6.8) and the browser rows of Phase 8A follow only behind their flags and approvals.
3. The first adapter is a **manual adapter**: the owner records approved supplier mappings and quote outcomes in HTCO's UI, with the same validation and audit as an automated adapter.
4. The owner asks AutoVA whether an API, export, webhook or partner integration exists. An API adapter is built if one does.
5. Browser automation of AutoVA is considered only through a new ADR, after checking AutoVA's terms, running in an isolated worker with no CAPTCHA or MFA bypass, read-only first.

## Consequences

- Positive: the order and tracking core works without any AutoVA integration; supplier workflows can start manually and become automatic later.
- Negative: supplier quote onboarding (Phase 13, phases renumbered by PLAN 2.0) stays semi-manual until AutoVA offers an integration.

## Validation

Phase 0: owner's answer from AutoVA recorded in [ASSUMPTIONS.md](../product/ASSUMPTIONS.md). Phase 6: tracking detection works with no AutoVA credentials at all.

## Validation log

- 2026-10-28 (audit): Decision 2 and the context corrected. AutoVA does not write tracking to Etsy for the pilot shop (A-05, answered 2026-10-23); the owner copies it by hand, and slice 6.10's relay covers the gap. HTCO's side of the decision is unchanged: tracking is read from Etsy, with no AutoVA credentials (Phase 6 slices 6.1 and 6.5).
