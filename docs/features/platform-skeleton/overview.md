# Platform skeleton — Overview

| | |
|---|---|
| **Slug** | `platform-skeleton` |
| **Phase / slices** | Phase 1 — slices 1.1 to 1.7 |
| **Requirements** | [FR-AUD-02, FR-AUD-03](../../product/PRD.md) |
| **Status** | In progress |
| **Last updated** | 2026-10-14 |

## What and why

The frame every later feature is built inside: a uv workspace whose packages follow the hexagonal layers, the quality gates that keep it that way (formatting, lint, strict types, architecture contracts, tests with coverage floors, secret scanning, commit rules), and, in later slices, a production-shaped API with health checks, CI, a container and a deployment. Adding gates before there is code is cheaper than retrofitting them.

## Users and actors

- **Developers and AI coding agents**, who get fast local feedback (`just check-fast`, Git hooks) and the same checks CI runs (`just check`).
- **The owner**, who reviews pull requests knowing every gate has run, and starts production deploys.
- **DigitalOcean health checks**, which call `/healthz` and `/readyz` (slice 1.2).

## Acceptance criteria

| ID | Criterion | Slice | Status |
|---|---|---|---|
| AC-1.1.1 | Given a clean checkout, when `just check` runs, then formatting, lint, type checks, architecture contracts and tests all run and pass. | 1.1 | Pass |
| AC-1.1.2 | Given `htco_domain` imports `fastapi`, when `just arch` runs, then it fails naming the contract. | 1.1 | Pass |
| AC-1.1.3 | Given Python 3.14 and the pinned LangGraph version, when the smoke test builds and runs a two-node graph with an in-memory checkpointer, then it passes. | 1.1 | Pass |
| AC-1.1.4 | Coverage below the floors (domain 95%, application 90%) fails the run. | 1.1 | Pass |
| AC-1.4.1 | A pull request with a failing test, lint error, type error, forbidden import, secret-like string or vulnerable pinned dependency cannot merge (`ci-gate` red). | 1.4 | Open |
| AC-1.4.2 | A pull request that adds `@pytest.mark.skip` without a "Test changes" section fails `test-integrity`. | 1.4 | Open |
| AC-1.4.3 | Every `uses:` line references a full commit SHA (checked by a script). | 1.4 | Open |

Slices 1.2 to 1.7 add their criteria here as they start; the full list is in the [phase plan](../../plan/phases/phase-01-walking-skeleton.md).

## Scope

- **In (1.1):** workspace and packages, Ruff, mypy, import-linter and the layer purity test, pytest with AnyIO and Hypothesis, coverage floors, `just` recipes, prek Git hooks (ruff, gitleaks, file hygiene, Conventional Commits).
- **In (1.4):** the CI workflow with one required status (`ci-gate`), secret and dependency scans, workflow linting, the test-weakening check, the pull request title check, and the Renovate configuration.
- **Out (1.1 and 1.4):** the API itself (1.2), the OpenAPI contract and its CI job (1.3), the container, its CI job and deployment (1.5), agent hooks and the branch ruleset (1.6), monitoring (1.7).

## External facts learned

- 2026-10-14: LangGraph 1.2.14 with langgraph-checkpoint 4.2.0 runs a checkpointed two-node graph on CPython 3.14.8, although its package metadata only declares up to 3.13 (assumption A-10 verified).
- 2026-10-14: `langsmith`, which LangGraph pulls in through `langchain-core`, calls `asyncio.iscoroutinefunction`, deprecated in Python 3.14 and due for removal in 3.16. It is silenced by one targeted warning filter; the dependency must be fixed upstream before we move to Python 3.16.
- 2026-10-14: `langsmith` registers a pytest plugin through entry points. Our pytest run loads only allow-listed plugins.
- 2026-10-14: uv records a relative `exclude-newer` as a span in `uv.lock` (`exclude-newer-span = "P7D"`), so the release-age rule does not make the lock stale.

## Known limits

- The domain and application packages contain no business code yet; their coverage floors start to bite with Phase 2.
- The import-linter contracts list only the packages that exist; each new package adds itself to the contracts in the slice that creates it.
