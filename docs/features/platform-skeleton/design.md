# Platform skeleton — Design

## Components and layers

| Layer | Package (workspace member) | Python package | Responsibility today |
|---|---|---|---|
| Domain | `packages/domain` (`htco-domain`) | `htco_domain` | Empty until Phase 2; standard library only |
| Application | `packages/application` (`htco-application`) | `htco_application` | Empty until Phase 2; standard library and domain only |
| Platform | `packages/platform` (`htco-platform`) | `htco_platform` | Settings, logging and health helpers from slice 1.2 |
| Adapter | `packages/agent-runtime` (`htco-agent-runtime`) | `htco_agent_runtime` | Pins LangGraph and proves it runs on our Python |
| Composition root | `apps/api` (`htco-api`) | `htco_api` | The HTTP API from slice 1.2 |

The workspace root is virtual (no package of its own). Every member has a fixed version `0.0.0` and depends on its inner layers through `{ workspace = true }` sources, so the lock resolves them from this checkout and never from an index.

## Architecture contracts

```mermaid
flowchart BT
  domain[htco_domain]
  application[htco_application]
  agent[htco_agent_runtime]
  platform[htco_platform]
  api[htco_api]
  application --> domain
  agent --> application
  api --> agent
  api --> application
  api --> platform
```

| Contract | Tool | Rule |
|---|---|---|
| `dependency-rule` | import-linter (layers) | `htco_api` → `htco_agent_runtime` → `htco_application` → `htco_domain`; nothing imports outward |
| `platform-is-infrastructure` | import-linter (forbidden) | `htco_platform` imports no business, adapter or app code |
| `inner-layers-without-platform` | import-linter (forbidden) | Domain and application never import `htco_platform` |
| `langgraph-only-in-agent-runtime` | import-linter (forbidden) | Only `htco_agent_runtime` imports `langgraph` or `langchain_core` |
| Domain imports only the standard library | `tests/architecture/test_layer_purity.py` | Every import statement (including inside functions and `TYPE_CHECKING`) |
| Application imports only the standard library and the domain | same test | same |
| Every package imports from this checkout | `tests/architecture/test_workspace_packages.py` | Catches packaging mistakes (wrong module name, stale installs) |

## Task runner and hooks

`just` recipes call tools through `uv run`, so the versions come from `uv.lock`; `just` itself is a dev dependency (`rust-just`). `check` = `lint` + `types` + `arch` + `test`; `check-fast` = `lint` + `types` + tests without coverage, for the agent hooks of slice 1.6.

Git hooks (prek, `.pre-commit-config.yaml`): file hygiene and private-key detection (prek built-ins), gitleaks pinned by commit SHA, Ruff format and lint, and `tools/conventional_commit.py` on `commit-msg`. CI re-runs all of them, so a skipped hook only delays the failure.

## Decisions and alternatives

| Decision | Alternatives considered | Why |
|---|---|---|
| All tests in one `tests/` tree, one folder per package | Tests inside each package (`packages/<pkg>/tests`) | With per-package test folders, mypy sees several top-level `tests` and `conftest` modules (and hyphenated folders stop its package search), so it needs one run per package. One tree gives unique module names, one fast mypy run and one pytest configuration. |
| Layer purity as an AST test | An import-linter forbidden list of third-party packages | A forbidden list is a deny-list that silently misses every new dependency; the allow-list (standard library plus inner layers) cannot be expressed in import-linter. |
| `coverage run -m pytest` with per-layer `--fail-under` reports | pytest-cov | One tool fewer; per-layer floors need separate reports anyway. |
| `rust-just` and `prek` in the dev group | Global installs | Contributors install only uv; CI uses the locked versions. |
| Own Conventional Commits checker | commitizen | Same rule in about 80 lines, no extra dependency, reused by the CI title check. |
| `exclude-newer = "7 days"` | Release age enforced only by Renovate | Applies to every resolution, including manual `uv lock --upgrade`. |
