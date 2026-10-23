# Phase 12 — Product Studio

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 11](phase-11-listing-health.md); decision D-04 (catalog model vs Etsy policy) |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-AUT-05, FR-CAT-01, FR-CAT-02, FR-CAT-03, FR-CAT-04, FR-CAT-05, FR-CAT-06, FR-CAT-07, FR-MDL-08 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 12.1 Catalog model and media provenance
- [ ] 12.2 Research and sourcing (Shon)
- [ ] 12.3 Policy screening (Shon, hard rules, Decision Advisor)
- [ ] 12.4 Copy (Noam) and images (Nika) in parallel
- [ ] 12.5 Pricing and catalog assembly (Daniel)
- [ ] 12.6 Risk-based review (Noa)
- [ ] 12.7 Catalog Draft Creation & Publishing automation
- [ ] 12.8 Shon and Nika activated
- [ ] 12.9 Product Studio UI

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- **Catalog Draft Creation & Publishing** runs every step through the gateway: `etsy.listings.create_draft` → `etsy.listing_media.manage` → `etsy.listing_properties.manage` → `etsy.inventory.update` → `etsy.listing_translations.manage` → approval → `etsy.listings.change_state` (W3, Owner, `financial`: activation charges the listing fee). Each step is verified by re-reading the draft.
- **Shop structure** writes are W2: `etsy.shipping_profiles.manage`, `etsy.return_policies.manage`, `etsy.processing_profiles.manage`, `etsy.sections.manage`, plus `etsy.listings.delete` (W3).
- **Research reads** are R0: `etsy.listings.search_public`, `etsy.shops.search` and the buyer taxonomy, for Shon. They are never copied into a listing and never used to train a model.
- Every write tool is owner-trigger first on each shop. No test writes to the live shop.

## Goal

One product goes from a research idea to a policy-compliant Etsy draft, and after approval to a live listing, through the Product Research, Content & Catalog department: **Shon** (Product Intelligence) researches, sources and screens; **Noam** writes the copy and **Nika** (Creative & Media) prepares the images, in parallel; **Daniel** prices in code and assembles the publish-ready catalog specification; **Noa** reviews by risk; the **Catalog Draft Creation & Publishing** automation (not an employee) creates the draft and publishes after approval.

## Why after Listing Health

Listing Health proved the runtime, the approval loop, the "no invented claims" validator and Daniel's and Noam's evaluations on existing listings. Product Studio reuses all of them and adds creation.

## Scope

**In:** products and variants, sources and provenance; Shon's research and sourcing through permitted sources only (facts separated from estimates); policy screening with hard rules first, then the Decision Advisor (if benchmarked for this family), then Shon, with the result `APPROVED │ REVIEW │ REJECTED` (plus `NOT_ALLOWED_ON_ETSY`), reasons, sources and confidence; Noam's copy (no invented claims); Nika's images, limited to permitted edits of real product photos with provenance; Daniel's pricing (Decimal math in code, margin floor; Daniel explains, the code computes) and catalog assembly; Noa's risk-based review; the Catalog Draft Creation & Publishing automation (draft, images, inventory, approval, activation, re-read).

**Out:** supplier quote onboarding ([Phase 13](phase-13-suppliers-finance.md)); ads and marketing ([Phase 21](phase-21-growth-scale.md)).

## Owner prerequisites

- D-04 decided: which product types are sold on Etsy and how they comply with Etsy's reselling, production-partner, photo and AI-disclosure rules.
- Pricing policy: margin floor, rounding, fee assumptions.
- Reconnect the shop if publishing needs scopes not yet granted (see ADR-0008).
- Labelled examples for the policy-screening benchmark (at least 200, FR-MDL-08) and Shon's and Nika's evaluation sets.
- Approval of Shon's (proposed L0) and Nika's (proposed L1) autonomy levels, and of the automation's activation.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Shon, Product Intelligence | [`shon.toml`](../../../registry/employees/shon.toml) |
| Employee | Nika, Creative & Media | [`nika.toml`](../../../registry/employees/nika.toml) |
| Automation | Catalog Draft Creation & Publishing | [`catalog-draft-publishing.toml`](../../../registry/automations/catalog-draft-publishing.toml) |

Daniel and Noam are already active (Phase 11); their sheets get a new profile version for the new responsibilities (pricing, catalog assembly, new-product copy), re-evaluated before use. Each employee follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7 (22-section sheet, evaluation, autonomy approval, routing measured); the automation follows the rule of [Phase 5](phase-05-orders-ingest.md) (18-section specification, replay, failure and recovery tests, runbook, approval).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 12.1 | Catalog model: products, variants, product sources, assets with provenance; media backup job for Supabase Storage with a restore test | Every asset has source and edit history; a restored backup contains every asset |
| 12.2 | Shon's research and sourcing with structured, cited outputs; research tools read untrusted pages with no write tools | Every claim cites a source; estimates labelled as estimates |
| 12.3 | Policy screening: hard rules in code, then the Decision Advisor where its benchmark passed, then Shon; current marketplace policy references | Benchmark of 200+ labelled candidates recorded; `NOT_ALLOWED_ON_ETSY` and `REJECTED` stop the pipeline; a model is never the only gate |
| 12.4 | Noam's copy and Nika's images in parallel; validator rejects invented attributes and misleading images | Zero unsupported claims on the evaluation set; every image keeps provenance |
| 12.5 | Daniel: pricing through `pricing.calculate` (Decimal, margin floor) and catalog assembly into a publish-ready specification | Property tests: price never below floor; no money arithmetic in a model |
| 12.6 | Noa's risk-based review of candidates and catalog specifications | Every product reaching approval has a recorded review or a recorded sampling decision |
| 12.7 | Catalog Draft Creation & Publishing: draft → images → inventory → approval → activate; verify by re-read; replaces the Phase 11 listing-update handler | Activation only after approval; listing fee charged only then; replaying a trigger creates no second draft |
| 12.8 | Shon and Nika activated; Daniel and Noam re-evaluated for their new responsibilities | Sheets defined, evaluations passed, autonomy approved, routing measured |
| 12.9 | Product Studio UI: pipeline board, product page, approvals, who did what from activity records | Pipeline state visible per product, from recorded activity only |

## Test plan

Graph tests with fakes; evaluation suites per employee (screening accuracy, unsupported-claim rate, image policy checks, pricing explanations matching the computed price); the screening benchmark; automation replay, failure and recovery tests; end-to-end draft creation against fixtures; the first real draft created in the owner's presence.

## Security checklist

- [ ] Rule of Two per graph; Shon reads untrusted pages with no write tools.
- [ ] No buyer data in any prompt.
- [ ] Publishing only through the automation and only after approval; no employee holds a publishing tool.
- [ ] Image rights and provenance checked before use.

## Documentation

`docs/features/product-studio/`, `docs/features/employees/shon.md`, `docs/features/employees/nika.md`; the automation specification completed; COST_MODEL updated with cost per approved listing.

## Exit gate

- [ ] One real product published end to end with approvals; cost per approved listing reported; evaluations and the screening benchmark meet thresholds.
- [ ] Shon, Nika and the automation are `ready` and approved; Daniel's and Noam's new profile versions passed their evaluations.

## Rollback

Employee kill switches; the automation's kill switch; drafts can be deleted on Etsy before activation; activated listings are deactivated through an approved action.

## Risks

| Risk | Mitigation |
|---|---|
| Policy screening misses a marketplace rule | Hard rules first; `REVIEW` routes uncertain cases to the owner; policy references re-verified at kickoff |
| Image edits misrepresent products | Permitted edits only; Noa reviews every image of high-risk products |

## Cost impact

AI per approved listing measured (planning range $0.10–0.50 excluding images); Etsy listing fees per activation.
