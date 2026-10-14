# ADR-0009: AutoVA behind a port, tracking read from Etsy

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The original planning brief assumes AutoVA places supplier orders automatically once a supplier offer is approved per product or variant, and that HTCO reads supplier quotes and tracking from it. A review of autova.io on 2026-10-14 found:

- AutoVA describes itself as an analytics and operations workspace for Etsy sellers: profit analytics, an AI listing studio, supplier sourcing, an order inbox, team roles. Etsy is the only working marketplace connection; others are "in progress".
- It writes tracking numbers to Etsy through the Etsy API when an order is marked shipped, and syncs orders every 15 minutes.
- Its own FAQ calls the automatic-ordering demonstration on its homepage a "concept demonstration" and asks users to confirm availability in their account.
- There is **no public API, webhook or developer documentation**.

## Decision

1. All AutoVA interaction goes through an `AutoVaPort` with operations for quotes, supplier mappings and order or tracking status.
2. **Tracking does not depend on AutoVA access**: AutoVA writes tracking to Etsy, and HTCO reads it from Etsy (ADR-0008).
3. The first adapter is a **manual adapter**: the owner records approved supplier mappings and quote outcomes in HTCO's UI, with the same validation and audit as an automated adapter.
4. The owner asks AutoVA whether an API, export, webhook or partner integration exists. An API adapter is built if one does.
5. Browser automation of AutoVA is considered only through a new ADR, after checking AutoVA's terms, running in an isolated worker with no CAPTCHA or MFA bypass, read-only first.

## Consequences

- Positive: the order and tracking core works without any AutoVA integration; supplier workflows can start manually and become automatic later.
- Negative: supplier quote onboarding (Phase 11) stays semi-manual until AutoVA offers an integration.

## Validation

Phase 0: owner's answer from AutoVA recorded in [ASSUMPTIONS.md](../product/ASSUMPTIONS.md). Phase 6: tracking detection works with no AutoVA credentials at all.

## Validation log

No entries yet.
