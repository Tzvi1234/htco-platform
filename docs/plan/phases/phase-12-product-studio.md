# Phase 12 — Product Studio and Creative Studio

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 11](phase-11-listing-health.md); the image adapter of [Phase 8](phase-08-models-tools.md); decision D-04 (catalog model vs Etsy policy) |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-AUT-05, FR-CAT-01, FR-CAT-02, FR-CAT-03, FR-CAT-04, FR-CAT-05, FR-CAT-06, FR-CAT-07, FR-MDL-08 |
| **Mandatory tests owned** | Test 11 (a catalog process that breaks a platform rule is blocked and reviewed, nothing published) and test 8 in real use (a Nika asset has provenance, price, permissions and `NEEDS_QA`); Phase 8 owns test 8 on the adapter ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 12.1 Catalog model and media provenance
- [ ] 12.2 Etsy selling-model policy gate (dropshipping and reselling)
- [ ] 12.3 Research and sourcing (Shon, full scope)
- [ ] 12.4 Policy screening (hard rules, Decision Advisor, Shon)
- [ ] 12.5 Nika's Creative Studio: images in real use
- [ ] 12.6 Copy (Noam) in parallel with images
- [ ] 12.7 Pricing and catalog assembly (Daniel)
- [ ] 12.8 Risk-based review (Noa)
- [ ] 12.9 Catalog Draft Creation & Publishing automation
- [ ] 12.10 Shon and Nika activated; Daniel and Noam re-evaluated
- [ ] 12.11 Product Studio and Creative Studio UI

## Goal

One product goes from a research idea to a policy-compliant Etsy draft, and after approval to a live listing, through the Product Research, Content & Catalog department: **Shon** (Product Intelligence) researches, sources and screens; **Noam** writes the copy and **Nika** (Creative & Media) prepares the images in her Creative Studio, in parallel; **Daniel** prices in code and assembles the publish-ready catalog specification; **Noa** reviews by risk; the **Catalog Draft Creation & Publishing** automation (not an employee) creates the draft and publishes after approval. Before anything is drafted or published, the product's selling model is checked against Etsy's dropshipping and reselling policy: a generic AliExpress or 1688 product is never assumed to be allowed.

## Why after Listing Health

Listing Health proved the Claude runtime on content work, the approval loop, the "no invented claims" validator and Daniel's and Noam's evaluations on existing listings. Product Studio reuses all of them and adds creation. Phase 8 built the image adapter, its provenance table and its tool with recorded fixtures; this phase puts them in Nika's hands for real ([ADR-0018](../../adr/0018-image-generation-specialist.md)). Shon already researched in the pilot of [Phase 7](phase-07-pilot-office.md); here he gets his full scope (sourcing and screening).

## Scope

**In:** products and variants, sources and provenance; the Etsy selling-model policy gate; Shon's research and sourcing through permitted sources only (facts separated from estimates); policy screening with hard rules first, then the Decision Advisor (if benchmarked for this family), then Shon, with the result `APPROVED │ REVIEW │ REJECTED` (plus `NOT_ALLOWED_ON_ETSY`), reasons, sources and confidence; Nika's Creative Studio (briefs, `media.generate_gpt` and `media.edit` through the tool bridge, rights and licence checks, provenance, cost, `NEEDS_QA` review); Noam's copy (no invented claims); Daniel's pricing (Decimal math in code, margin floor; Daniel explains, the code computes) and catalog assembly; Noa's risk-based review; the Catalog Draft Creation & Publishing automation (draft, images, inventory, approval, activation, re-read); Product Studio and Creative Studio screens (clients specification screens 7, 8, 9, 10 and 11, first version).

**Out:** supplier quote onboarding ([Phase 13](phase-13-procurement-finance.md)); ads and marketing ([Phase 21](phase-21-growth-scale.md)); the complete office screens ([Phase 16](phase-16-web-office.md)).

## Owner prerequisites

- D-04 decided: which product types are sold on Etsy and how they comply with Etsy's current dropshipping, reselling, production-partner, photo and AI-disclosure rules (read from Etsy's help centre and Seller Policy at kickoff, never from memory).
- Pricing policy: margin floor, rounding, fee assumptions.
- Reconnect the shop if publishing needs scopes not yet granted (see [ADR-0008](../../adr/0008-etsy-integration.md)).
- OpenAI API organization, service key for images and a monthly image budget (ADR-0018, ADR-0017 rules); never a ChatGPT subscription.
- Labelled examples for the policy-screening benchmark (at least 200, FR-MDL-08), including known-forbidden selling models, and Shon's and Nika's evaluation sets.
- Approval of Shon's (proposed L0) and Nika's (proposed L1) autonomy levels, and of the automation's activation.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. Load `design-system` before any work in `htco-clients` (slice 12.11).
2. Read this file, then [PLAN.md](../PLAN.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ADR-0018](../../adr/0018-image-generation-specialist.md), [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), [ADR-0013](../../adr/0013-automation-engine.md), the Phase 8 file (image adapter, `asset_provenance`, tool registry and MCP gateway), the Phase 11 file, `registry/employees/shon.toml`, `nika.toml`, `daniel.toml`, `noam.toml`, `registry/automations/catalog-draft-publishing.toml`, `registry/tools/creative_media.toml` and the D-04 record in ASSUMPTIONS.md.
3. Refresh the facts at kickoff: Etsy's dropshipping and reselling policy, production-partner and AI-disclosure rules, listing creation and image upload endpoints and fees; the image model ID, price and limits from the model catalog (verified against the provider in Phase 8, re-checked now). Record the policy references with their retrieval date; they are inputs to the hard rules.
4. Work in this order: 12.1 → 12.2 → 12.3 → 12.4 → 12.5 and 12.6 in parallel → 12.7 → 12.8 → 12.9 → 12.10 → 12.11. The policy gate (12.2) must exist before any code path can create a draft.
5. Every employee runs as Claude-runtime steps: one session per bounded step of one task, started from the task brief, with only its granted HTCO tools in the `htco` MCP server; handoffs between Shon, Noam, Nika and Daniel are tasks plus `employee_messages`. Nika's session is an Anthropic model; images are made only by her tools calling `ImageGenerationPort`, which return asset references, never bytes.
6. Never: let any employee hold a publishing tool; publish or draft a product whose selling-model check is missing, stale or not `ALLOWED`; publish an asset in `NEEDS_QA`; hard-code an image model ID or price; send buyer data in a prompt or image input; let a model be the only gate for a marketplace rule.
7. Stop and ask the owner when D-04 is not decided, when Etsy's policy changed since D-04, when a product falls in `REVIEW`, when the screening benchmark misses its threshold, or when an image would show something the catalog facts do not support.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Shon, Product Intelligence (full scope; research-only pilot since Phase 7) | [`shon.toml`](../../../registry/employees/shon.toml) |
| Employee | Nika, Creative & Media | [`nika.toml`](../../../registry/employees/nika.toml) |
| Automation | Catalog Draft Creation & Publishing | [`catalog-draft-publishing.toml`](../../../registry/automations/catalog-draft-publishing.toml) |

Daniel and Noam are already active (Phase 11); their sheets get a new profile version for the new responsibilities (pricing, catalog assembly, new-product copy), re-evaluated before use. Each employee follows the employee activation rule of [PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7 as applied in [Phase 10](phase-10-fulfillment-incidents.md) slice 10.10 (22-section sheet with `runtime = "claude"`, evaluation, autonomy approval, routing measured); the automation follows the rule of [Phase 4](phase-04-durable-core-order-intake.md) (18-section specification, replay, failure and recovery tests, runbook, approval).

## Slices

### 12.1 — Catalog model and media provenance

Products, variants, product sources and assets; every asset links to an `asset_provenance` row (from Phase 8) or to its original source; a media backup job for Supabase Storage with a restore test.

| ID | Acceptance criterion |
|---|---|
| AC-12.1.1 | Every asset has a source and an edit history; an asset without either cannot be attached to a catalog specification. |
| AC-12.1.2 | A restored media backup contains every asset referenced by a product. |

### 12.2 — Etsy selling-model policy gate (dropshipping and reselling)

A deterministic gate in code, run before screening finishes and again as a precondition of every draft and publish step: each product records its selling model (from D-04's allowed list, for example made or designed by the seller, with or without a disclosed production partner), the supplier relationship, and the Etsy policy references with their retrieval date; the gate returns `ALLOWED`, `REVIEW` or `NOT_ALLOWED_ON_ETSY` with reasons. A product sourced as a generic AliExpress or 1688 item with no allowed selling model is `NOT_ALLOWED_ON_ETSY`. Policy references older than the configured age (proposed: 90 days) make the gate return `REVIEW`.

| ID | Acceptance criterion |
|---|---|
| AC-12.2.1 | A product with no recorded selling model, a model outside D-04's list, or stale policy references cannot reach draft creation (property tests over the gate). |
| AC-12.2.2 | The gate is code with versioned rules; no model output can change its result, and the Decision Advisor and Shon only add reasons or move `ALLOWED` to `REVIEW`, never the other way. |
| AC-12.2.3 | Every gate result is stored with the rule version, the policy references and the actor, and shown on the product page. |

### 12.3 — Research and sourcing (Shon, full scope)

Shon's research and sourcing steps with structured, cited outputs (`research.web_search`, `research.fetch_page`, `suppliers.search`, `catalog.read`); research tools treat pages as untrusted data and Shon holds no write tools. Each candidate is a task; each source read is a bounded step; the candidate and its evidence are written to Postgres before the step ends.

| ID | Acceptance criterion |
|---|---|
| AC-12.3.1 | Every claim cites a source; estimates are labelled as estimates. |
| AC-12.3.2 | Injection text in a supplier or competitor page changes no tool, grant, memory item or output schema (test 14 fixtures on research content). |

### 12.4 — Policy screening (hard rules, Decision Advisor, Shon)

Hard rules in code (12.2 and the other marketplace rules: prohibited items, intellectual property flags, required disclosures), then the Decision Advisor where its benchmark passed in Phase 8 for this family, then Shon's reasoning; current marketplace policy references.

| ID | Acceptance criterion |
|---|---|
| AC-12.4.1 | A benchmark of 200+ labelled candidates is recorded; the advisor is used for this family only if it beat rules-only on false-approve rate. |
| AC-12.4.2 | `NOT_ALLOWED_ON_ETSY` and `REJECTED` stop the pipeline; `REVIEW` creates an approval for the owner; a model is never the only gate. |

### 12.5 — Nika's Creative Studio: images in real use

Nika works from a brief task: she reads the catalog facts and source assets (`catalog.read`, `media.read_asset`), checks rights and licences, calls `media.generate_gpt` or `media.edit` through the tool bridge (names per ADR-0018; reconcile with the registry's current `media.generate_image` and `media.edit_image` in the profile version written here), and validates the result against the facts (colour, scale, included parts). The tool reserves budget, calls `ImageGenerationPort`, stores the image in Supabase Storage with an `asset_provenance` row and returns asset references. Image generation appears only in Nika's Creative Studio, never as an extra employee.

| ID | Acceptance criterion |
|---|---|
| AC-12.5.1 | **Mandatory test 8 in real use.** An asset Nika generates for a real product has an `asset_provenance` row (brief, input asset IDs and hashes, provider, model reference from the catalog, parameters, cost, task, licence check result), a cost entry settled against the reserved budget, permissions limited to the shop and roles that may see it, and the state `NEEDS_QA`; the publishing automation refuses it until a person (or, after her evaluation, the agreed review path) approves it. |
| AC-12.5.2 | Prompts and image inputs contain no buyer data (PII scanner on every request in tests and in a production sample). |
| AC-12.5.3 | An image that contradicts a catalog fact (wrong colour, scale, material or included parts) is rejected by the validation step or by review and never reaches a catalog specification. |
| AC-12.5.4 | A provider refusal, 429, budget exhaustion or timeout moves Nika's task to `WAITING_EXTERNAL` or fails it with a typed reason; no other provider is tried unless an approved fallback route exists. |

### 12.6 — Copy (Noam) in parallel with images

Noam writes new-product copy from verified facts only, in parallel with 12.5 as a separate task; the 11.4 validator rejects invented attributes.

| ID | Acceptance criterion |
|---|---|
| AC-12.6.1 | Zero unsupported claims on the evaluation set; every claim in an approved specification traces to a fact. |

### 12.7 — Pricing and catalog assembly (Daniel)

Pricing through `pricing.calculate` (Decimal, margin floor) and catalog assembly into a publish-ready specification: copy, approved assets, variants, price, selling-model gate result.

| ID | Acceptance criterion |
|---|---|
| AC-12.7.1 | Property tests: price never below the floor; no money arithmetic in a model; Daniel's explanation matches the computed price. |
| AC-12.7.2 | A specification cannot be assembled with an asset in `NEEDS_QA` or a gate result other than `ALLOWED`. |

### 12.8 — Risk-based review (Noa)

Noa reviews candidates and catalog specifications by risk (her pilot scope from Phase 7, extended here by a new profile version): every high-risk product and every generated image of a high-risk product, plus a sample of the rest.

| ID | Acceptance criterion |
|---|---|
| AC-12.8.1 | Every product reaching approval has a recorded review or a recorded sampling decision. |

### 12.9 — Catalog Draft Creation & Publishing automation

Draft → images → inventory → approval → activate; verify by re-read; replaces the Phase 11 listing-update handler. Preconditions on every step: the selling-model gate is `ALLOWED` and fresh, every asset is approved (not `NEEDS_QA`), the specification passed the claims validator, and the approval is decided.

| ID | Acceptance criterion |
|---|---|
| AC-12.9.1 | **Mandatory test 11.** A catalog run whose product breaks a platform rule (fixtures: generic reseller item with no allowed selling model; prohibited item; missing required disclosure; an unapproved generated image) is blocked at the precondition, opens a review (approval or quality finding) with the rule and evidence, and makes zero listing-create, image-upload or activate calls to Etsy (strict HTTP mock). |
| AC-12.9.2 | Activation only after approval; the listing fee is charged only then; replaying a trigger creates no second draft. |
| AC-12.9.3 | After activation, the re-read listing matches the approved specification; otherwise an incident opens. |

### 12.10 — Shon and Nika activated; Daniel and Noam re-evaluated

Owner-labelled evaluation sets; screening accuracy and false-approve rate for Shon; image policy checks and fact-consistency for Nika; new profile versions for Daniel and Noam.

| ID | Acceptance criterion |
|---|---|
| AC-12.10.1 | Shon's and Nika's sheets have no `draft` section, `runtime = "claude"`, evaluations passed, autonomy approved, routing measured. |
| AC-12.10.2 | Nika's routing policy allows only Anthropic models for her session; OpenAI is reached only through her image tools under the image budget. |
| AC-12.10.3 | Daniel's and Noam's new profile versions passed their evaluations before the new responsibilities are used. |

### 12.11 — Product Studio and Creative Studio UI

Pipeline board, product page (gate results with policy references, screening, copy, assets, price), approvals, and Nika's Creative Studio (briefs, generations, image input records, licence check, provenance, cost, review status); who did what from activity records.

| ID | Acceptance criterion |
|---|---|
| AC-12.11.1 | Pipeline state is visible per product, from recorded activity only; employee states come from task runs. |
| AC-12.11.2 | Every generated image shows its provenance, cost and `NEEDS_QA` or approved state; the approve action needs the right role. |

## Test plan

Runtime tests with the fake model gateway and recorded responses for every employee step; evaluation suites per employee (screening accuracy, unsupported-claim rate, image policy and fact checks, pricing explanations matching the computed price); the screening benchmark; property tests for the policy gate and pricing; image adapter contract tests (from Phase 8) re-run with the current catalog entry; automation replay, failure and recovery tests; mandatory tests 8 and 11; end-to-end draft creation against fixtures; the first real draft created in the owner's presence.

## Security checklist

- [ ] Rule of Two per employee step type; Shon reads untrusted pages with no write tools.
- [ ] No buyer data in any prompt or image input.
- [ ] Publishing only through the automation and only after approval; no employee holds a publishing tool.
- [ ] Image rights, licence and provenance checked before use; the OpenAI key is a separate secret with its own budget and kill switch.
- [ ] Selling-model gate and policy references re-verified at kickoff and on a schedule.

## Documentation

`docs/features/product-studio/`, `docs/features/creative-studio/`, `docs/features/employees/shon.md`, `docs/features/employees/nika.md`; the automation specification completed; ADR-0018 validation log (test 8 in real use); D-04 outcome and policy references in ASSUMPTIONS.md; COST_MODEL updated with cost per approved listing and per image.

## Demo

Take one real product idea from Shon's research to a live listing: the selling-model gate result with its Etsy policy references, the screening, Noam's copy, Nika's image with its provenance and QA approval, Daniel's price, Noa's review, the owner's approval, the activation and the re-read. Then run mandatory test 11 with a forbidden product and show that nothing reached Etsy.

## Exit gate

- [ ] All AC-12.x pass, including mandatory tests 8 (real use) and 11.
- [ ] One real product published end to end with approvals; cost per approved listing reported; evaluations and the screening benchmark meet thresholds.
- [ ] Shon, Nika and the automation are `ready` and approved; Daniel's and Noam's new profile versions passed their evaluations.

## Rollback

Employee kill switches; the automation's kill switch; the image provider's kill switch; drafts can be deleted on Etsy before activation; activated listings are deactivated through an approved action.

## Risks

| Risk | Mitigation |
|---|---|
| The selling model does not fit Etsy's policy | D-04 before kickoff; the gate blocks by default; `REVIEW` routes uncertain cases to the owner |
| Policy screening misses a marketplace rule | Hard rules first; policy references re-verified at kickoff; a model is never the only gate |
| Image edits misrepresent products | Permitted edits only; fact validation; `NEEDS_QA` until approved; Noa reviews every image of high-risk products |
| Image costs grow | Per-image budget reservation, Nika's per-task cap, image kill switch |

## Cost impact

AI per approved listing and per image measured at the gate (no figure assumed before the evaluation run; image prices come from the verified catalog); Etsy listing fees per activation.
