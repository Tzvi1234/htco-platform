# ADR-0017: AuthAndBillingPolicy — how every agent run is authenticated and billed

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Source:** HTCO OS Claude-Native Hybrid Edition, master plan v6.0, section 1

## Context

The owner has a personal Claude subscription and will create an Anthropic API organization. These are different things.

- Anthropic's support article (updated 7 October 2026) says the Agent SDK and `claude -p` can draw on a Claude plan's usage limits.
- The Agent SDK documentation says third-party developers may not offer claude.ai login or plan rate limits in their products unless Anthropic approved it in advance. Products must use API key authentication.
- Max and Team plans may include monthly API credits. These help pay for API usage, but they do not make a consumer subscription usable as product infrastructure.

HTCO is a business system that runs unattended on DigitalOcean. Getting this wrong risks the account, the terms of service and the business.

## Decision drivers

- Compliance with Anthropic's terms, with no "clever" workarounds.
- An unattended runtime must never depend on a personal login.
- Running out of quota or credit must never stop order intake, the ledger or the deterministic monitors.
- Every run records which billing mode paid for it, so costs are never invented.

## Decision

A single domain policy, `AuthAndBillingPolicy`, decides for every agent run which credential and billing mode it may use. Three modes:

| Mode | Where | Credential | Default |
|---|---|---|---|
| `API_PRODUCTION` | The `agent-worker` on DigitalOcean (development, staging, production) | An Anthropic API key from a dedicated service account in the HTCO API organization, one key per environment, stored as an encrypted DigitalOcean secret and read only by the Claude runtime adapter | **On.** The only mode for hosted runs |
| `SUBSCRIPTION_INTERACTIVE` | A developer's own Claude Code sessions on their own machine: building HTCO, experiments, reviews | The developer's own login, used interactively under their plan's terms | Allowed for development only. Never reachable from the product, never in a worker, never through extracted or shared OAuth tokens or session cookies |
| `APPROVED_SUBSCRIPTION_RUNTIME` | Reserved | Only if Anthropic approves in writing the exact use in this private office, and the security, environment and quota tests pass | **Off** behind a feature flag. Turning it on needs a new ADR that links the written approval |

Rules:

1. The policy lives in `htco_domain.billing` (pure) with the decision in `htco_application.billing`. The Claude runtime adapter asks it before every session and refuses to start without an allowed mode. The mode is stored on `agent_runs.billing_mode`.
2. Credentials never reach a client, a prompt, a log, a tool result or another worker. There is no API that returns a key. Rotation is a runbook procedure.
3. **Quota and credit exhaustion are normal states.** On 429, `error_max_budget_usd`, an exhausted budget or a provider outage, the task moves to `WAITING_FOR_EXTERNAL` with a retry time, or to an approved fallback route. Automations, the ledger and the deterministic monitors keep running because they make no model calls ([ER-AI-01](../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai)). Nothing fails silently.
4. **No rate-limit evasion.** No rotating keys to dodge limits, no sharing sessions between workers, no scraping credentials. Concurrency is set by account limits and budget.
5. **Emergency stop.** The global kill switch and a per-mode switch stop new runs within 60 seconds. When a mode is disabled the UI shows it as disabled, with the reason.
6. **Honest cost display.** API cost comes from `model_usage` and the price catalog. For subscription sessions (development only), the UI never invents per-token cost when the provider exposes no reliable billing data. API credits are shown separately from paid usage.

## Options considered

1. **API key for everything hosted; subscription for personal development only (chosen).** Compliant and predictable, but costs money per token.
2. **Run hosted workers on the owner's subscription.** Cheaper on paper, but forbidden for products without prior approval, fragile (one personal login), and it shares limits with the owner's own work. Rejected.
3. **Subscription now, API later.** Builds the product on a path that may have to be torn out. Rejected.

## Consequences

- Positive: compliant by construction; costs are real and attributable per employee, task and shop; quota problems degrade service instead of stopping it.
- Negative: model use costs API money from the first pilot run. The monthly budget (owner decision D-06) caps it.
- Follow-ups: the owner creates the Anthropic API organization, service account and keys (Phase 0); Phase 6 implements the policy and its tests; Phase 8 connects it to the budget ledger and the AI cost screen.

## Validation

- Tests: a worker configured with a subscription mode refuses to start a session; a session without an allowed mode is refused; 429 and budget exhaustion move tasks to waiting while an order-intake test keeps passing (master plan test 3); `billing_mode` is present on every `agent_runs` row.
- Phase 0: the owner confirms the API organization and service account exist. Their IDs (not keys) are recorded in `ASSUMPTIONS.md`.

## Validation log

- 2026-10-14 — Statement in the Agent SDK overview verified: claude.ai login and plan limits are not allowed in third-party products without prior approval.
