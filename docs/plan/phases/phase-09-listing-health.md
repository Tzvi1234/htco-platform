# Phase 9 — Listing Health (first AI workflow)

| | |
|---|---|
| **Milestone** | **M3 — MVP** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 8; decision D-03 |
| **Requirements** | FR-AGT-06, FR-CAT-04, FR-QA-01, FR-QA-02, FR-QA-03 |
| **Status** | Not started |

## Progress

- [ ] 9.1 Listing sync
- [ ] 9.2 Product facts
- [ ] 9.3 Audit graph
- [ ] 9.4 Fixes through approvals
- [ ] 9.5 Gold dataset and evals
- [ ] 9.6 Listing Health UI

## Goal

Every active listing is audited against verified product facts. Where a listing over-promises (size, material, included items, colour, misleading photos), the owner sees the evidence and a concrete fix, approves it, and the fix is applied on Etsy and verified. This is the first agent workflow and the MVP milestone.

## Scope

**In:** listing sync (read), product facts with sources, the `listing_health` graph (deterministic checks first, then model-assisted claim comparison), findings with evidence, fixes as approvals with before/after diffs, applying approved fixes through the outbox and re-reading to verify, gold dataset and evals, Listing Health UI.

**Out:** creating new listings (Phase 10), image generation.

## Owner prerequisites

- Product facts for the pilot catalog (dimensions, material, what is included), from supplier pages or AutoVA; the UI supports entering them.
- Labeling of a sample of listings (about 30) for the gold dataset.
- Reconnect the shop to grant `listings_w` (re-consent; see ADR-0008).

## Slices

### 9.1 — Listing sync

Read active listings, inventory, images and properties into `listings` and `listing_variants`; listing webhooks if available, otherwise scheduled sync.

| ID | Acceptance criterion |
|---|---|
| AC-9.1.1 | All active listings of the pilot shop are present with images and variations after one sync; repeated syncs are idempotent. |

### 9.2 — Product facts

`product_facts` with source (supplier page, AutoVA, owner) and confidence; entry and import UI; facts linked to listings.

| ID | Acceptance criterion |
|---|---|
| AC-9.2.1 | Every fact shows its source and when it was recorded; facts without a source cannot be saved. |

### 9.3 — Audit graph

Deterministic checks (stated dimensions vs facts, material keywords, item counts in title vs included items, missing scale reference in photos); model-assisted extraction of claims from title, description and tags (structured output, balanced tier) and comparison to facts; risk score per listing; findings with cited evidence; proposed fixes (title, description, attributes, photo order) that never invent facts.

| ID | Acceptance criterion |
|---|---|
| AC-9.3.1 | Every finding cites the listing field and the fact it contradicts; a finding without evidence is discarded. |
| AC-9.3.2 | Proposed text contains no claim absent from the facts (checked by a validator before the approval is created). |
| AC-9.3.3 | Injection text inside a listing description or supplier page does not change the graph's tools or output schema. |
| AC-9.3.4 | Cost per audited listing is recorded. |

### 9.4 — Fixes through approvals

Each fix becomes an approval with a before/after diff; on approval, `listing.update` runs through the outbox; the listing is re-read to verify; a failed or partial write opens an incident.

| ID | Acceptance criterion |
|---|---|
| AC-9.4.1 | No Etsy listing changes without an approved approval (audit proves it). |
| AC-9.4.2 | After applying, the stored listing matches the approved "after" text; otherwise an incident opens. |

### 9.5 — Gold dataset and evals

Owner-labeled sample; precision and recall of high-risk findings; thresholds agreed (proposed: precision ≥ 85% on high-risk findings); nightly evals; results shown in the agent directory.

| ID | Acceptance criterion |
|---|---|
| AC-9.5.1 | The eval report meets the agreed thresholds before the audit is scheduled automatically. |

### 9.6 — Listing Health UI

Dashboard (risk distribution, top findings, cost), finding detail with evidence and photos, approve and reject with reasons (reasons feed the dataset), applied status.

| ID | Acceptance criterion |
|---|---|
| AC-9.6.1 | The diff view is readable in Hebrew and English UI with English listing text kept LTR. |

## Test plan

Deterministic checks with table-driven and property tests; graph tests with a fake gateway and recorded model responses; injection corpus; evals; end-to-end approval-to-Etsy flow against fixtures; manual verification on the real shop with one low-risk fix first.

## Security checklist

- [ ] Rule of Two: the audit agent reads untrusted content and sees no sensitive data; external writes only after human approval.
- [ ] No buyer data in prompts.
- [ ] Etsy policy references for listing content re-verified at phase start.

## Documentation

`docs/features/listing-health/`; MVP success criteria recorded; COST_MODEL updated with cost per audited listing.

## Demo — Milestone M3 (MVP)

Run the audit on the whole shop; walk through the top findings; approve one fix and see it live on Etsy; show the eval report and the cost.

## Exit gate

- [ ] MVP success criteria in [MVP.md](../../product/MVP.md#4-mvp-success-criteria) all met; owner signs off the MVP.

## Rollback

Disable the audit schedule; applied fixes have stored "before" versions and can be reverted through the same approval flow.

## Risks

| Risk | Mitigation |
|---|---|
| Too few product facts to audit meaningfully | Start with listings that have facts; the UI shows coverage |
| False positives annoy the owner | Precision threshold before automation; reject reasons improve the dataset |

## Cost impact

AI cost per audited listing, measured; Batch API for full-catalog runs.
