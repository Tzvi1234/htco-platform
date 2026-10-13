# ADR-0001: Two repositories, platform and clients

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner (H.T.Z Studios), engineering

## Context

HTCO OS has a Python backend with workers and an agent runtime, plus three native clients (web in TypeScript, Windows in C#, Android in Kotlin). The original planning brief considered one repository per agent, one per service, and a single monorepo.

## Decision drivers

- Clear boundaries between the system of record and its clients.
- Toolchains and release cycles that differ by stack.
- Low overhead for a very small team working with AI agents.
- One source of truth for contracts and business rules.

## Options considered

1. **One repository for everything.** One place, but four toolchains in every CI run, mixed release cadences, and a large context for every agent session.
2. **One repository per service or agent.** Maximum isolation, but heavy coordination; agents are profiles, not services.
3. **Two repositories: `htco-platform` and `htco-clients`.** Backend and its docs in one; all clients, design tokens and generated API clients in the other.

## Decision

Option 3. `htco-platform` owns the API, domain, workers, agent runtime, migrations, contracts, infrastructure and the system-level documentation. `htco-clients` owns the web, Windows and Android apps, design tokens and components, and the generated API clients.

## Consequences

- Positive: each repository has one coherent toolchain family and focused CI; contract changes are explicit (the platform publishes, clients pin and regenerate).
- Negative: a feature that spans API and UI needs two pull requests; the clients' copy of the OpenAPI document can lag.
- Follow-ups: a contract job in `htco-clients` fails when the pinned OpenAPI copy and the generated clients drift; the platform's CI runs a breaking-change check on every API change.

## Validation

Revisit if cross-repository coordination becomes the main source of friction, or when a separate team takes over one of the clients.
