# Phase 10 — Product Studio

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 9; decision D-04 (catalog model vs Etsy policy) |
| **Requirements** | FR-AGT-06, FR-CAT-01, FR-CAT-02, FR-CAT-03, FR-CAT-04, FR-CAT-05, FR-CAT-06, FR-CAT-07 |
| **Status** | Not started — acceptance criteria refined at kickoff (rolling wave) |

## Progress

- [ ] 10.1 Catalog model: products, variants, product sources, assets with provenance; media backup job for Supabase Storage with a restore test
- [ ] 10.2 Research and sourcing agents (L0) with structured, cited outputs
- [ ] 10.3 Classifier policy gate with current marketplace policy references
- [ ] 10.4 Copy and image steps in parallel; validator rejects invented attributes and misleading images
- [ ] 10.5 Pricing in code: cost, shipping, fees, margin floor; agent explains only
- [ ] 10.6 Supervisor QA and catalog assembly
- [ ] 10.7 Publishing: draft → images → inventory → approval → activate; verify by re-read
- [ ] 10.8 Product Studio UI: pipeline board, product page, approvals

## Goal

One product goes from a research idea to a policy-compliant Etsy draft, and after approval to a live listing, through the Research & Catalog department: research, sourcing, classification gate, copy and images in parallel, catalog assembly, pricing, supervisor QA and publishing.

## Why after Listing Health

Listing Health proves the agent runtime, the approval loop and the "no invented claims" validator on existing listings. Product Studio reuses all three and adds creation.

## Scope

**In:** products and variants, sources and provenance; Competitor Research and Product Sourcer agents (permitted sources only, facts separated from estimates); Product Classifier with `APPROVED │ REVIEW │ REJECTED │ NOT_ALLOWED_ON_ETSY`; SEO Copywriter (no invented claims); Image Creator limited to permitted edits of real product photos with provenance; Catalog Architect; Pricing Analyst (Decimal math in code, margin floor); Supervisor QA; Publishing Agent creating drafts, uploading images and inventory, activation only after approval.

**Out:** supplier quote onboarding (Phase 11), ads.

## Owner prerequisites

- D-04 decided: which product types are sold on Etsy and how they comply with Etsy's reselling, production-partner, photo and AI-disclosure rules.
- Pricing policy: margin floor, rounding, fees assumptions.
- Reconnect the shop if publishing needs scopes not yet granted (see ADR-0008).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 10.1 | Catalog model: products, variants, product sources, assets with provenance; media backup job for Supabase Storage with a restore test | Every asset has source and edit history; a restored backup contains every asset |
| 10.2 | Research and sourcing agents (L0) with structured, cited outputs | Every claim cites a source; estimates labeled as estimates |
| 10.3 | Classifier policy gate with current marketplace policy references | Gold set accuracy threshold; `NOT_ALLOWED_ON_ETSY` blocks the pipeline |
| 10.4 | Copy and image steps in parallel; validator rejects invented attributes and misleading images | Zero unsupported claims on the eval set |
| 10.5 | Pricing in code: cost, shipping, fees, margin floor; agent explains only | Property tests: price never below floor; Decimal only |
| 10.6 | Supervisor QA and catalog assembly | QA checklist recorded per product |
| 10.7 | Publishing: draft → images → inventory → approval → activate; verify by re-read | Activation only after approval; listing fee charged only then |
| 10.8 | Product Studio UI: pipeline board, product page, approvals | Pipeline state visible per product |

## Test plan

Graph tests with fakes; eval suites per agent (classification accuracy, unsupported-claim rate, image policy checks); end-to-end draft creation against fixtures; first real draft created in the owner's presence.

## Security checklist

- [ ] Rule of Two per graph; research agents read untrusted pages with no write tools.
- [ ] No buyer data in any prompt.
- [ ] Publishing tool requires approval (L2) until evals justify a pre-approved policy.

## Exit gate

- [ ] One real product published end to end with approvals; `cost_per_listing` reported; evals meet thresholds.

## Cost impact

AI per listing measured (planning range $0.10–0.50 excluding images); Etsy listing fees per activation.
