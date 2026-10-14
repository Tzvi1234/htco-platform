# Phase 11 — Listing Health

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 10](phase-10-fulfillment-incidents.md); decision D-03 |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-CAT-04, FR-QA-01, FR-QA-02, FR-QA-03 |
| **Mandatory tests owned** | None of the fifteen ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); re-runs the prompt-injection fixtures of test 14 on listing and supplier content |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; its acceptance criteria are refined at kickoff with what Phases 6–10 taught us (pilot evaluation results, Eitan's first weeks). The criteria below are the minimum.

## Progress

- [ ] 11.1 Listing sync
- [ ] 11.2 Product facts
- [ ] 11.3 Accuracy audit (Daniel)
- [ ] 11.4 Fix copy (Noam)
- [ ] 11.5 Fixes through approvals and the approved write path
- [ ] 11.6 Noa's sampling review
- [ ] 11.7 Daniel and Noam activated
- [ ] 11.8 Listing Health UI

## Goal

Every active listing is audited against verified product facts. Where a listing over-promises (size, material, included items, colour, misleading photos), the owner sees the evidence and a concrete fix, approves it, and the fix is applied on Etsy and verified. **Daniel** (Catalog & Pricing Architect) runs the accuracy audit against supplier and owner facts; **Noam** (SEO & Copywriting) writes the corrected copy from those facts only; **Noa** reviews a risk-based sample within her pilot scope from [Phase 7](phase-07-pilot-office.md). Fixes reach Etsy only through an approved, deterministic write path, never by a model writing to Etsy. This is the first content workflow (decision D-03).

## Why now

The Claude runtime, the router, budgets and the pilot's supervision exist (Phases 6–8), and Eitan has run in production on real orders (Phase 10). Listing Health is low-risk to start (read first, write only after approval), valuable for an existing shop, and it proves the "no invented claims" validator that Product Studio reuses in [Phase 12](phase-12-product-studio.md).

## Scope

**In:** listing sync (read); product facts with sources; Daniel's audit (deterministic checks first, then model-assisted claim extraction and comparison); findings with evidence; Noam's fix copy with the claims validator; fixes as approvals with before/after diffs; applying approved fixes through the outbox and re-reading to verify; Noa's sampling; gold dataset and evaluations; Daniel's and Noam's activation on the Claude runtime; Listing Health UI (clients specification screen 10, Listing Workspace, first version).

**Out:** creating new listings ([Phase 12](phase-12-product-studio.md)); image generation or editing (Nika, Phase 12); pricing changes (Daniel's pricing work starts in Phase 12).

## Owner prerequisites

- Product facts for the pilot catalog (dimensions, material, what is included), from supplier pages or AutoVA; the UI supports entering them.
- Labelling of a sample of listings (about 30) for the gold dataset.
- Reconnect the shop to grant `listings_w` (re-consent; see [ADR-0008](../../adr/0008-etsy-integration.md)).
- Approval of Daniel's and Noam's autonomy levels (proposed: L1).

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. Load `design-system` before any work in `htco-clients` (slice 11.8).
2. Read this file, then [PLAN.md](../PLAN.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ADR-0008](../../adr/0008-etsy-integration.md), [ADR-0014](../../adr/0014-tool-registry.md), the [Phase 6](phase-06-workforce-claude-runtime.md) and [Phase 8](phase-08-models-tools.md) files, `registry/employees/daniel.toml` and `noam.toml`, `registry/tools/commerce_catalog.toml`, `docs/product/MVP.md` and the Etsy listing policies current at kickoff.
3. Refresh the facts at kickoff: Etsy listing update endpoints and scopes, Etsy policy references for listing content. Record them in `docs/features/listing-health/`.
4. Work in this order: 11.1 → 11.2 → 11.3 (deterministic checks first, then Daniel's steps) → 11.4 → 11.5 → 11.6 → 11.7 → 11.8.
5. Daniel and Noam run as Claude-runtime steps: one session per bounded step of one task (audit one listing; write copy for one finding), started from the task brief in Postgres, with only their granted HTCO tools in the `htco` MCP server. Handoff from Daniel to Noam is a task handoff plus an `employee_messages` row, never shared session state. Results, evidence and state summaries go to Postgres before the session ends.
6. Never: give any employee a tool that writes a live listing; let model output reach Etsy without an approval; store listing text from a model that the validator rejected; send buyer data to a model.
7. Stop and ask the owner when fact coverage is too low to audit meaningfully, when precision on the gold sample misses the threshold, or when an Etsy policy reference changed since kickoff.

## Employees activated

| Employee | Registry | Work in this phase |
|---|---|---|
| Daniel, Catalog & Pricing Architect | [`daniel.toml`](../../../registry/employees/daniel.toml) | Accuracy audit against facts; risk score; findings with evidence |
| Noam, SEO & Copywriting | [`noam.toml`](../../../registry/employees/noam.toml) | Corrected titles, descriptions, tags and attributes from verified facts only |

Each follows the employee activation rule of [PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7, as applied in [Phase 10](phase-10-fulfillment-incidents.md) slice 10.10: all 22 sections of the specification sheet completed from `draft` to `defined` with `runtime = "claude"`; the evaluation set built with the Phase 6 harness and passed; the owner's approval of the autonomy level; the routing policy measured.

## Slices

### 11.1 — Listing sync

Read active listings, inventory, images and properties into `listings` and `listing_variants` through `MarketplacePort`; listing webhooks if available, otherwise scheduled sync.

| ID | Acceptance criterion |
|---|---|
| AC-11.1.1 | All active listings of the shop are present with images and variations after one sync; repeated syncs are idempotent. |

### 11.2 — Product facts

`product_facts` with source (supplier page, AutoVA, owner) and confidence; entry and import UI; facts linked to listings.

| ID | Acceptance criterion |
|---|---|
| AC-11.2.1 | Every fact shows its source and when it was recorded; facts without a source cannot be saved. |

### 11.3 — Accuracy audit (Daniel)

Deterministic checks first (stated dimensions vs facts, material keywords, item counts in title vs included items, missing scale reference in photos). Then one task per listing for Daniel: a step reads the listing and its facts through `catalog.read`, extracts claims from title, description and tags as structured output (schema from his sheet), compares them with facts, and records findings with cited evidence and a risk score before ending the step with `task.complete` or `task.handoff` to Noam.

| ID | Acceptance criterion |
|---|---|
| AC-11.3.1 | Every finding cites the listing field and the fact it contradicts; a finding without evidence is discarded. |
| AC-11.3.2 | Injection text inside a listing description or supplier page does not change Daniel's tools, grants or output schema (test 14 fixtures applied to listing content). |
| AC-11.3.3 | Cost per audited listing is recorded in `model_usage` from the SDK's usage data, linked to the task and the `agent_runs` row. |
| AC-11.3.4 | A full-catalog audit run as many tasks survives a worker replacement mid-run: finished listings are not audited twice, unfinished ones restart from their task brief. |

### 11.4 — Fix copy (Noam)

For each accepted finding, Noam receives a handoff task and proposes the corrected title, description, tags or attributes using only verified facts (`catalog.read`, `catalog.update_draft_content` on the finding's draft only); a validator rejects any claim absent from the facts before an approval is created. Photo order and scale reference changes are proposed as instructions, not generated images.

| ID | Acceptance criterion |
|---|---|
| AC-11.4.1 | Proposed text contains no claim absent from the facts (checked by the validator before the approval is created). |
| AC-11.4.2 | Noam's output is a draft attached to the finding; he has no tool that writes to Etsy. |
| AC-11.4.3 | The handoff from Daniel to Noam is one task and one stored, acknowledged `employee_messages` row with the same correlation ID; Noam's session starts from the task brief, not from Daniel's session. |

### 11.5 — Fixes through approvals and the approved write path

Each fix becomes an approval with a before/after diff. On approval, a deterministic listing-update handler (the `listings.apply_approved_fix` tool definition in `registry/tools/commerce_catalog.toml`, executed only by the approval's execution and never granted to an employee; folded into the Catalog Draft Creation & Publishing automation in Phase 12) applies it through the outbox, re-reads the listing to verify, and opens an incident on a failed or partial write.

| ID | Acceptance criterion |
|---|---|
| AC-11.5.1 | No Etsy listing changes without an approved approval (audit and activity records prove it). |
| AC-11.5.2 | After applying, the stored listing matches the approved "after" text; otherwise an incident opens. |
| AC-11.5.3 | The write path is unreachable from any employee session (tool bridge test over every employee grant). |

### 11.6 — Noa's sampling review

Quality Review Scheduling (active since Phase 7) adds Listing Health to Noa's queue: every applied fix on a high-risk listing and a sample of the rest; Noa checks the finding, the copy and the applied result against facts and the re-read listing, and records quality findings. Her full nightly supervision is [Phase 14](phase-14-governance.md).

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
| AC-11.8.2 | Employee states on the screen come only from recorded task runs, never from a model's statement. |

## Test plan

Deterministic checks with table-driven and property tests; runtime tests for Daniel's and Noam's steps with the fake model gateway and recorded model responses; injection corpus; evaluations for Daniel and Noam; end-to-end approval-to-Etsy flow against fixtures; manual verification on the real shop with one low-risk fix first.

## Security checklist

- [ ] Rule of Two: the audit reads untrusted content and sees no sensitive data; external writes only after human approval and only through the deterministic write path.
- [ ] No buyer data in prompts.
- [ ] Daniel's and Noam's session options pass the Phase 6 CI check (no built-in tools, `setting_sources=[]`, never `bypassPermissions`).
- [ ] Etsy policy references for listing content re-verified at phase start.

## Documentation

`docs/features/listing-health/`, `docs/features/employees/daniel.md`, `docs/features/employees/noam.md`; success criteria of [MVP.md](../../product/MVP.md#4-mvp-success-criteria) checked; COST_MODEL updated with cost per audited listing.

## Demo

Run the audit on the whole shop; walk through the top findings with Daniel's evidence and Noam's fix; approve one fix and see it live on Etsy; show Noa's review, the evaluation reports and the cost.

## Exit gate

- [ ] All AC-11.x pass; the success criteria in [MVP.md](../../product/MVP.md#4-mvp-success-criteria) that apply to Listing Health are met; owner signs off.
- [ ] Daniel and Noam are `ready`, their autonomy approved, their routing measured.

## Rollback

Disable the audit schedule; Daniel's and Noam's kill switches; applied fixes have stored "before" versions and can be reverted through the same approval flow.

## Risks

| Risk | Mitigation |
|---|---|
| Too few product facts to audit meaningfully | Start with listings that have facts; the UI shows coverage |
| False positives annoy the owner | Precision threshold before automation; reject reasons improve the dataset |
| Full-catalog runs are expensive | Deterministic checks first; one bounded step per listing; per-task caps; batch processing where the router allows it (verify at kickoff) |

## Cost impact

AI cost per audited listing and per fix, measured at the gate; no figure assumed before the evaluation run.
