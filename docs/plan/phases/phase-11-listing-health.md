# Phase 11 — Listing Health (first content workflow)

| | |
|---|---|
| **Milestone** | **M3 — MVP** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 10](phase-10-supervision.md); decision D-03; optional: [Phase 8A](phase-08a-browser-infrastructure.md) for views and visits per listing from the stats export (B-02) |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-CAT-04, FR-QA-01, FR-QA-02, FR-QA-03 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; its acceptance criteria are refined at kickoff with what Phases 8–10 taught us. The criteria below are the minimum.

## Progress

- [ ] 11.1 Listing sync
- [ ] 11.2 Product facts
- [ ] 11.3 Accuracy audit (Daniel)
- [ ] 11.4 Fix copy (Noam)
- [ ] 11.5 Fixes through approvals and the approved write path
- [ ] 11.6 Noa's sampling review
- [ ] 11.7 Daniel and Noam activated
- [ ] 11.8 Listing Health UI

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- **API reads:** listing sync and audits use the API tools (`etsy.listings.read`, `etsy.listing_details.read`, `etsy.inventory.read`, `etsy.taxonomy.read`, `etsy.listing_shipping.read`, `etsy.policies.read`, `etsy.reviews.read`).
- **Listing changes from webhooks:** `listing.updated` from slice 5.3 triggers a re-audit of the listing it names.
- **Writes:**
  - the approved write path of 11.5 is `etsy.listings.update`, `etsy.listing_properties.manage` and `etsy.listing_translations.manage` (W1/W2), through the gateway, verified by re-reading the listing;
  - the planned `listings.apply_approved_fix` tool stays as the automation-facing wrapper;
  - each write tool is owner-trigger only on each shop until the owner's first verified run.
- **AC-11.5.3, amended by ADR-0022:** Daniel and Noam may hold write tools in the registry, but every call becomes an approval and runs outside the session. The write path stays unreachable from inside a session.
- **Combined processes:** where scoped and flagged, the stats export (B-02, Phase 8A) adds views, visits and conversion per listing to the audit, as a deterministic browser recipe. Without it, the audit uses the API data only and says so.

## Goal

Every active listing is audited against verified product facts. Where a listing over-promises (size, material, included items, colour, misleading photos), the owner sees the evidence and a concrete fix, approves it, and the fix is applied on Etsy and verified. **Daniel** (Catalog & Pricing Architect) runs the accuracy audit against supplier and owner facts; **Noam** (SEO & Copywriting) writes the corrected copy from those facts only; **Noa** reviews a risk-based sample. Fixes reach Etsy only through an approved, deterministic write path, never by a model writing to Etsy. This is the first content workflow and the MVP milestone.

## Why now

The runtime, router, budgets and supervision exist (Phases 8–10), and one employee (Eitan) has run in production. Listing Health is low-risk to start (read first, write only after approval), valuable for an existing shop, and it proves the "no invented claims" validator that Product Studio reuses in [Phase 12](phase-12-product-studio.md).

## Scope

**In:** listing sync (read), product facts with sources, Daniel's audit (deterministic checks first, then model-assisted claim extraction and comparison), findings with evidence, Noam's fix copy with the claims validator, fixes as approvals with before/after diffs, applying approved fixes through the outbox and re-reading to verify, Noa's sampling, gold dataset and evaluations, Daniel's and Noam's activation, Listing Health UI.

**Out:** creating new listings ([Phase 12](phase-12-product-studio.md)), image generation or editing (Nika, Phase 12), pricing changes (Daniel's pricing work starts in Phase 12).

## Owner prerequisites

- Product facts for the pilot catalog (dimensions, material, what is included), from supplier pages or AutoVA; the UI supports entering them.
- Labelling of a sample of listings (about 30) for the gold dataset.
- Reconnect the shop to grant `listings_w` (re-consent; see ADR-0008).
- Approval of Daniel's and Noam's autonomy levels (proposed: L1).

## Employees activated

| Employee | Registry | Work in this phase |
|---|---|---|
| Daniel, Catalog & Pricing Architect | [`daniel.toml`](../../../registry/employees/daniel.toml) | Accuracy audit against facts; risk score; findings with evidence |
| Noam, SEO & Copywriting | [`noam.toml`](../../../registry/employees/noam.toml) | Corrected titles, descriptions, tags and attributes from verified facts only |

Each follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7: all 22 sections of the specification sheet completed from `draft` to `defined`; the evaluation set built and passed; the owner's approval of the autonomy level; the routing policy measured.

## Slices

### 11.1 — Listing sync

Read active listings, inventory, images and properties into `listings` and `listing_variants`; listing webhooks if available, otherwise scheduled sync.

| ID | Acceptance criterion |
|---|---|
| AC-11.1.1 | All active listings of the pilot shop are present with images and variations after one sync; repeated syncs are idempotent. |

### 11.2 — Product facts

`product_facts` with source (supplier page, AutoVA, owner) and confidence; entry and import UI; facts linked to listings.

| ID | Acceptance criterion |
|---|---|
| AC-11.2.1 | Every fact shows its source and when it was recorded; facts without a source cannot be saved. |

### 11.3 — Accuracy audit (Daniel)

Deterministic checks first (stated dimensions vs facts, material keywords, item counts in title vs included items, missing scale reference in photos); then Daniel extracts claims from title, description and tags (structured output, routed by his policy) and compares them with facts; risk score per listing; findings with cited evidence.

| ID | Acceptance criterion |
|---|---|
| AC-11.3.1 | Every finding cites the listing field and the fact it contradicts; a finding without evidence is discarded. |
| AC-11.3.2 | Injection text inside a listing description or supplier page does not change Daniel's tools, grants or output schema. |
| AC-11.3.3 | Cost per audited listing is recorded from `model_usage`. |

### 11.4 — Fix copy (Noam)

For each accepted finding, Noam proposes the corrected title, description, tags or attributes using only verified facts; a validator rejects any claim absent from the facts before an approval is created. Photo order and scale reference changes are proposed as instructions, not generated images.

| ID | Acceptance criterion |
|---|---|
| AC-11.4.1 | Proposed text contains no claim absent from the facts (checked by the validator before the approval is created). |
| AC-11.4.2 | Noam's output is a draft attached to the finding; he has no tool that writes to Etsy. |

### 11.5 — Fixes through approvals and the approved write path

Each fix becomes an approval with a before/after diff. On approval, a deterministic listing-update handler applies it through the outbox (planned: a new tool definition added to `registry/tools/` with an owner and tests, executed only by the approval's execution and never granted to an employee; folded into the Catalog Draft Creation & Publishing automation in Phase 12), re-reads the listing to verify, and opens an incident on a failed or partial write. No employee has a tool that writes a live listing.

| ID | Acceptance criterion |
|---|---|
| AC-11.5.1 | No Etsy listing changes without an approved approval (audit and activity records prove it). |
| AC-11.5.2 | After applying, the stored listing matches the approved "after" text; otherwise an incident opens. |
| AC-11.5.3 | The write path is unreachable from any employee session (tool bridge test over every employee grant). |

### 11.6 — Noa's sampling review

Quality Review Scheduling adds Listing Health to Noa's queue: every applied fix on a high-risk listing and a sample of the rest; Noa checks the finding, the copy and the applied result against facts and records findings.

| ID | Acceptance criterion |
|---|---|
| AC-11.6.1 | Every applied fix on a high-risk listing is reviewed by Noa; her findings link to the approval and the re-read result. |

### 11.7 — Daniel and Noam activated

Owner-labelled gold sample; precision and recall of high-risk findings (proposed: precision ≥ 85%); unsupported-claim rate of Noam's copy (target: zero on the evaluation set); both sheets completed; autonomy approved; routing measured.

| ID | Acceptance criterion |
|---|---|
| AC-11.7.1 | The evaluation reports meet the agreed thresholds before the audit is scheduled automatically. |
| AC-11.7.2 | Daniel's and Noam's sheets have no `draft` section; their autonomy approvals are recorded; cost per audited listing and per fix is within their policies' per-task caps. |

### 11.8 — Listing Health UI

Dashboard (risk distribution, top findings, cost), finding detail with evidence and photos, approve and reject with reasons (reasons feed the dataset), applied status, who did what (Daniel, Noam, Noa, the owner) from activity records.

| ID | Acceptance criterion |
|---|---|
| AC-11.8.1 | The diff view is readable in Hebrew and English UI with English listing text kept LTR. |

## Test plan

Deterministic checks with table-driven and property tests; graph tests with a fake gateway and recorded model responses; injection corpus; evaluations for Daniel and Noam; end-to-end approval-to-Etsy flow against fixtures; manual verification on the real shop with one low-risk fix first.

## Security checklist

- [ ] Rule of Two: the audit reads untrusted content and sees no sensitive data; external writes only after human approval and only through the deterministic write path.
- [ ] No buyer data in prompts.
- [ ] Etsy policy references for listing content re-verified at phase start.

## Documentation

`docs/features/listing-health/`, `docs/features/employees/daniel.md`, `docs/features/employees/noam.md`; MVP success criteria recorded; COST_MODEL updated with cost per audited listing.

## Demo — Milestone M3 (MVP)

Run the audit on the whole shop; walk through the top findings with Daniel's evidence and Noam's fix; approve one fix and see it live on Etsy; show Noa's review, the evaluation reports and the cost.

## Exit gate

- [ ] MVP success criteria in [MVP.md](../../product/MVP.md#4-mvp-success-criteria) all met; owner signs off the MVP.
- [ ] Daniel and Noam are `ready`, their autonomy approved, their routing measured.

## Rollback

Disable the audit schedule; Daniel's and Noam's kill switches; applied fixes have stored "before" versions and can be reverted through the same approval flow.

## Risks

| Risk | Mitigation |
|---|---|
| Too few product facts to audit meaningfully | Start with listings that have facts; the UI shows coverage |
| False positives annoy the owner | Precision threshold before automation; reject reasons improve the dataset |

## Cost impact

AI cost per audited listing, measured; Batch API for full-catalog runs.
