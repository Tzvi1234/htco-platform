# ADR-0023: Per-employee routing mode (routed or pinned) and automatic difficulty assessment

- **Status:** Accepted (owner approved the direction, 2026-10-26)
- **Date:** 2026-10-26
- **Deciders:** Owner, engineering
- **Amends:** [ADR-0015](0015-decision-advisor-and-routing.md) (a routing policy gains a mode; the task's difficulty may be assessed when it is not declared)
- **Related:** [ADR-0004](0004-model-gateway.md), [ADR-0012](0012-living-office-organization.md), [ADR-0019](0019-unified-execution-engine-and-action-gateway.md), FR-MDL-02, FR-MDL-05, FR-MDL-08, [Phase 9](../plan/phases/phase-09-router-advisor.md) slices 9.2, 9.3, 9.6, 9.8 and 9.9

## Context

ADR-0015 gives every employee a versioned `ModelRoutingPolicy`. One deterministic router raises the tier to the task's difficulty, within the policy's maximum. It then picks the cheapest healthy catalog model that fits, and records the decision. Two gaps remain:

1. **A fixed model.** For some employees, the owner wants one model, every time. A writer's voice, an evaluation that must stay comparable across weeks, or a model he trusts for one kind of work are examples. Today the only lever is to set the default and maximum tier to the same value. The router may still pick a different model within that tier when prices or health change, so the model is not fixed.
2. **Undeclared difficulty.** `RoutingRequest.minimum_tier` must be filled by the caller ("as judged by rules or the advisor"). Nothing in the plan says who judges a task that arrives without one. ADR-0015 notes that "Jev may judge task difficulty when rules cannot", without the conditions.

The constraints are unchanged. No model ID appears in code or in a policy; the catalog maps names to verified models. Every call goes through the Model Gateway, with budget reservation, PII masking and recording. Advisors never authorize. Rules come first.

## Decision drivers

- The owner chooses, per employee. Only he does. The choice is versioned and audited.
- A pinned call has the same controls as a routed one: gateway, budget, masking, recording, data-class and provider limits.
- A pinned model that becomes unavailable never silently becomes a different model. It uses a fallback the owner named, or it stops.
- Difficulty can only raise the tier, and never above the employee's maximum tier.
- Jev judges difficulty only after it passes its benchmark on this exact family.
- One router, inside the gateway: no new network service on the critical path.

## Options considered

1. **Pin by tier only (`default_tier = max_tier`).** No new concept, but it doesn't fix the model (see Context).
2. **Pin by provider model ID in the policy.** Simple, but it breaks the rule that no model ID appears outside the catalog. A deprecation would then need a change to every employee.
3. **Pin by catalog alias** (chosen). A stable name such as `default-advanced` that the catalog maps to exactly one verified model. A deprecation or upgrade is one catalog pull request, recorded with its as-of date. Policies stay free of model IDs.
4. **A separate router service or a hosted proxy (LiteLLM-style) as the router.** It adds a network hop and a second place for keys, budgets and logs. It is kept only as a possible future provider adapter behind the gateway (below).

For difficulty: **(a)** the caller must always declare it. That is brittle for agents. **(b)** Always ask a model. That is costly and unbenchmarked. **(c)** Rules first, then Jev "Ultrafast" only where benchmarked, else the employee's default (chosen).

## Decision

### 1. Routing mode per employee

A routing policy has a `mode`:

- **`routed`** is the default, and ADR-0015's design unchanged. The tier comes from the task's difficulty, from the policy's `default_tier` up to its `max_tier`. The router picks the cheapest healthy model that fits.
- **`pinned`**: the policy names one **catalog alias** (`pinned_model`, for example `default-advanced`), never a model ID. Every call of that employee uses the model the alias maps to.

**What does not change in pinned mode:** the call still goes through the Model Gateway, the budget reservation and settlement (9.4), PII masking (AC-9.1.3) and recording (one `routing_decisions` row and one `model_usage` row per call). The policy's limits still apply, and a call that breaks one is **refused**, not re-routed:
- allowed providers;
- required capabilities;
- maximum data class;
- per-task budget;
- the alias's tier must not be above the policy's `max_tier`.

**Difficulty in pinned mode.** The model does not change with difficulty. Difficulty is still assessed and recorded. When it is above the alias's tier, the decision records `difficulty_above_pinned`, so the owner can see an under-powered pin. A task that **declares** a hard minimum tier above the alias's tier is refused, as today.

**Ill-health in pinned mode.** The alias's model counts as unavailable when any of these holds:
- its catalog row is unhealthy (AC-9.2.3);
- its provider's circuit is open, or its kill switch is on (AC-9.1.4);
- the alias points to a retired row.

Then the router tries the policy's **`pinned_fallback`** aliases, in order. Each must meet the same limits. If the list is empty, or none is available, the call **stops**: it is refused with reason `pinned-unavailable`, the refusal is recorded, and the task follows its own failure path (a retry later, or escalation to a person). Pinned mode never falls back to routed selection, or to "the cheapest model in the tier".

**Who chooses.** The registry declares each employee's mode (default `routed`). In the 9.8 settings screen, **only the Owner** can switch an employee between `routed` and `pinned`, and choose the alias and its fallback list. The change needs an `aal2` session with step-up, and an `Idempotency-Key`. The choice is stored in `employee_routing_settings`. The change:
- writes a new `employee_profile_versions` row, with origin `owner_setting`;
- writes one audit entry: actor, employee, from → to, the new profile version, no personal data.

An Admin sees the mode read-only. No employee, agent or automation has a tool to change any routing setting. A registry sync that changes an employee's other fields carries the Owner's current setting into the new profile version. The Owner's choice wins over the registry's mode until he sets it back to "follow the registry". Every routing decision keeps the profile version it used (AC-9.3.4).

### 2. Automatic difficulty assessment

When a task does not declare its difficulty, the gateway's routing step assesses it **once per task**, before `route`:

1. **Rules decide first.** These are deterministic and make no model call. Some task families have a fixed difficulty in the registry. There are also generic rules on the request's shape: input size, number of steps, attachments, required capabilities such as vision.
2. **Then Jev "Ultrafast"**, only if three things hold: the rules are inconclusive; the `task_difficulty` family passed the 9.6 benchmark and is enabled; and the employee's `decision_advisor` is not `never`. Jev gets a `DecisionQuestion` of kind `choice` with options `simple`, `normal` and `complex`. Its context is structured task features plus a short, **PII-masked** task summary (at most `internal` data class; never buyer names, contacts, addresses or message text). It runs under the advisor's latency budget from 9.6. The answer is schema-validated and recorded as a `decision_records` row. Its cost is reserved and settled against the employee's and the task's budget.
3. **Otherwise the default:** the employee's `default_tier`. A timeout, an invalid answer or a provider failure also falls back to the default. The task never waits on the assessor.

The labels map to minimum tiers: `simple` → `economy`, `normal` → `balanced`, `complex` → `advanced`. `critical` is only ever **declared** by a task or rule, never assessed. An **assessed** difficulty can only raise the tier, from the policy's `default_tier` upward, and it is **clamped at the policy's `max_tier`**. It can never lead to a model above the employee's maximum tier, and a clamp is recorded as `difficulty_clamped`. A **declared** minimum above the maximum tier is refused, as today: the task needs a stronger employee, or a person. Each routing decision records the difficulty, its source (`declared`, `rules`, `advisor` or `default`) and whether it was clamped.

The advisor's answer stays advice. It never changes permissions, approvals, budgets or the policy's limits (ADR-0015).

### 3. No separate router server

The router stays a pure function, called inside the Model Gateway's process. Phase 9 adds no network service for routing.

A **LiteLLM-style proxy behind the gateway** remains a possible future provider adapter. Examples are a self-hosted proxy that fronts several providers, or one used for local models. HTCO's router, budget ledger, PII masking and recording stay in front of it. The proxy appears in the catalog as one provider. Its keys stay in the gateway's own secret scope. Adopting it needs its own ADR.

### 4. Domain and schema impact (for the Phase 9 slices; no code in this ADR)

**`htco_domain.routing`:**
- `RoutingMode` (`routed`, `pinned`).
- `TaskDifficulty` (`simple`, `normal`, `complex`), with the tier mapping above.
- `DifficultySource` (`declared`, `rules`, `advisor`, `default`).

**`ModelRoutingPolicy` gains three fields:**
- `mode: RoutingMode = ROUTED`;
- `pinned_model: str | None = None` (a catalog alias);
- `pinned_fallback: tuple[str, ...] = ()` (aliases).

**New invariants:**
- `pinned` requires `pinned_model`.
- `routed` forbids both pinned fields.
- Alias names match `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, at most 64 characters.
- No alias appears twice across `pinned_model` and `pinned_fallback`.

That the alias exists, and that its tier is within `max_tier`, is checked against the catalog. The registry check does it at sync, and the router does it again at each call.

**`htco_application.routing`:**
- `RoutingRequest` carries a `DifficultyAssessment` (tier, label, source, clamped) instead of a bare `minimum_tier`.
- `RoutingDecision` gains `mode`, `alias`, `difficulty`, `difficulty_source`, `difficulty_clamped` and `difficulty_above_pinned`.
- A new `assess_difficulty(task, *, rules, advisor, policy)` returns the assessment.
- `route` resolves a pinned alias through the catalog.

**Registry, `registry/employees/<id>.toml`, `[routing]`:**
- `mode = "routed" | "pinned"` (optional; absent means `routed`);
- `pinned_model = "<alias>"` (pinned only);
- `pinned_fallback = ["<alias>", ...]` (pinned only; empty means stop).

The registry tool refuses: an unknown mode; a pinned policy without an alias; an alias that is not in the catalog seed or is above `max_tier`; pinned fields on a routed policy. Every existing employee stays `routed` with no file change.

**Data (`DATA_MODEL`, Phase 9):**
- `model_catalog_aliases`: one row per alias, pointing to one `model_catalog` row, with an as-of date and a source; changed only by pull request.
- `employee_routing_settings`: the Owner's current mode, alias and fallback per employee. RLS: the Owner writes through the API; members read their organization's rows.
- `routing_decisions` gains the decision fields above.
- `employee_profile_versions` records each version's origin (`registry_sync` or `owner_setting`).

## Consequences

- **Positive.**
  - The owner can fix a model per employee without a model ID in code. Model upgrades remain one catalog change.
  - Tasks without a declared difficulty route sensibly, and cheap tasks stay cheap.
  - Jev's role in routing is benchmarked before use.
- **Negative.**
  - One more owner setting to review.
  - A pinned employee depends on one model's availability, so its work stops when the pin and its fallbacks are down. That is by design: the owner chose it.
  - The `task_difficulty` family needs its own labelled set, of at least 200 tasks.
- **Follow-ups (Phase 9).**
  - Aliases in the catalog (9.2).
  - Modes and assessment in the router (9.3).
  - The `task_difficulty` benchmark (9.6).
  - The Owner's setting in the settings UI (9.8).
  - Assessment in simple-versus-complex routing (9.9).
  - Observability of pins and clamps in the Decision & Model Observatory (Phase 15).

## Validation

These are the new acceptance criteria in Phase 9: AC-9.2.4–9.2.6, AC-9.3.5–9.3.11, AC-9.6.5, AC-9.8.3–9.8.5 and AC-9.9.3–9.9.4. Scenario E is extended: a pinned employee and a routed employee get the same task shape.

## Validation log

- 2026-10-26: Direction approved by the owner (routed or pinned per employee; difficulty from rules, then benchmarked Jev; never above the employee's cap; no separate router server).
