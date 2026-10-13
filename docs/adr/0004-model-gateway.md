# ADR-0004: Own model gateway on official SDKs

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The original planning brief forbids binding models to agent names or putting routing policy only in prompts, and requires a dynamic model catalog, budgets, fallbacks and a decision log. Candidate models named in the brief (Claude Opus, Sonnet and Haiku 5.5; GPT 5.6 Sol, Terra and Luna; Jev) are inputs to benchmarking, not commitments. Model catalogs change quickly: OpenAI's model list moved from GPT-5.6 to GPT-6.1 in under three months ([OpenAI changelog](https://developers.openai.com/api/docs/changelog), checked 2026-10-14).

## Decision drivers

- Provider independence and quick catalog changes without code changes.
- Exact usage and cost accounting, including cache tokens.
- Native structured outputs.
- Small attack surface: the gateway handles provider credentials.

## Options considered

1. **LiteLLM proxy service.** Broad provider coverage, but several critical 2026 vulnerabilities (including pre-authentication SQL injection and remote code execution, listed by CISA as known exploited) and a compromised PyPI release in March 2026 ([LiteLLM security update](https://docs.litellm.ai/blog/security-update-march-2026), [CSA research note](https://labs.cloudsecurityalliance.org/research/csa-research-note-litellm-rce-cve-2026-42271-20260609/)).
2. **LiteLLM or Pydantic AI as a library.** Less code to write; adds a fast-moving dependency in the most sensitive path.
3. **Our own gateway with thin adapters on the official Anthropic and OpenAI SDKs.**

## Decision

Option 3:

- `ModelGateway` port; requests name a capability tier (`economy`, `balanced`, `advanced`, `critical`, `vision`, `image`), data class, budget and output schema, never a model.
- A deterministic, versioned router chooses `NO_AI` or a tier and a fallback chain from the **model catalog**, which is data (provider model ID, API version, prices with as-of date, limits, health, eval scores), seeded and changed by pull request.
- A **budget ledger** reserves estimated cost before each call and settles actual cost after; caps at organization, shop, agent, task, day and month levels.
- Every call writes `routing_decisions` and `model_usage`.
- Provider quirks stay in adapters (for example, limits on forcing a tool choice with some models and settings); callers only see structured outputs.
- A separate `DecisionPort` hosts experimental typed-decision providers (TypeSafe's Jev, OpenAI's Decisions API) behind a structured-output fallback, only after benchmarking. The site referenced in the original brief (jevtypesafeai.com) states in its [terms](https://jevtypesafeai.com/terms) that it is not affiliated with TypeSafe AI, so it is not used.
- A library such as LiteLLM or Pydantic AI may later become one more adapter, exact-pinned with hashes; we never run a gateway proxy.

## Consequences

- Positive: exact costs, minimal dependencies in the credential path, catalog changes without deployments.
- Negative: we write and maintain adapters and price data ourselves.
- Follow-ups: model catalog seed and price verification at the start of Phase 8; eval harness to fill catalog quality scores.

## Validation

Phase 8 tests: budget cap refuses calls, fallback chain works when a provider fails, every call is logged with cost, and no package outside `htco_model_router` imports a provider SDK.

## Validation log

No entries yet.
