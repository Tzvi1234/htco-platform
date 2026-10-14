# ADR-0018: Image generation as a specialist adapter owned by Nika

- **Status:** Accepted; validated in Phase 8 (adapter) and Phase 12 (first real use)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Source:** HTCO OS Claude-Native Hybrid Edition, master plan v6.0, section 5; clients specification v6.0, sections 8 and 13

## Context

Product listings need images: variations, backgrounds and edits of real product photos. The master plan assigns this to Nika (Creative & GPT Image Specialist) and names the OpenAI Image API as the engine. The Claude runtime ([ADR-0016](0016-claude-native-agent-runtime.md)) runs Anthropic models only, so image generation cannot be "the session switching to GPT".

## Decision

- **Nika is the accountable employee** for every generated or edited image: brief, source facts, rights check, provenance, quality checks and cost. No other employee generates images.
- **The engine is an adapter.** `ImageGenerationPort` in `htco_application` (operations `generate` and `edit`, typed request with brief, source asset references, size and count, data class and budget). `OpenAIImageAdapter` in `htco_model_router` is the first implementation, the only place the OpenAI SDK is imported for images. The model ID, price and limits come from the model catalog, verified against the provider before use; no ID is hard-coded.
- **Reached as a tool.** Nika's session calls `media.generate_gpt` and `media.edit` through the tool bridge, like any other tool. The tool reserves budget, calls the port, stores the result in Supabase Storage with a provenance record (`asset_provenance`: prompt brief, input asset IDs and hashes, provider, model ref, parameters, cost, created by, task, licence check result) and returns asset references, never raw bytes into the session.
- **Every generated asset starts as `NEEDS_QA`.** Publishing uses only approved assets. Images must not misrepresent the product (wrong colour, size, material or included parts); this is checked against the catalog facts and by a person until Nika's evaluation passes.
- **No ChatGPT subscription as an API.** Only the OpenAI API with a service key, under the same rules as [ADR-0017](0017-auth-and-billing-policy.md) (separate key, budget, emergency stop).
- **Privacy.** No buyer personal data in image prompts or inputs.
- **UI.** Image generation appears in Nika's Creative Studio. It is never shown as an extra employee (clients specification §13).

## Options considered

1. **Adapter behind a port, used through Nika's tools (chosen).** Same governance as every other provider call.
2. **A separate "image employee".** Rejected: the specification forbids invented extra staff, and the accountability belongs to Nika.
3. **Generating images inside the Claude session.** Not possible with the Claude runtime, and it would bypass the catalog and budget.

## Consequences

- Positive: one accountable owner, provenance for every asset, and a provider that can be swapped behind the port.
- Negative: one more provider key and budget to manage.
- Follow-ups: Phase 8 builds the port, adapter, provenance table and tool with recorded fixtures; Phase 12 runs Nika's evaluation and the Creative Studio.

## Validation

Master plan test 8: a generated asset has provenance, a recorded price, the right permissions and the `NEEDS_QA` state; publishing refuses it until approved. Contract tests with recorded fixtures cover 400, 429, 5xx, content-policy refusals and timeouts.

## Validation log

- 2026-10-14 — Decision recorded. Provider model IDs and prices are not verified yet (catalog entries are verified in Phase 8).
