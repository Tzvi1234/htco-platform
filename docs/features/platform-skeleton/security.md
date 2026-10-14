# Platform skeleton — Security

## Data flow and trust boundaries

Slice 1.1 has no runtime data flow. Its trust boundary is the software supply chain: packages from PyPI, hooks from GitHub, and code written by people and AI agents.

```mermaid
flowchart LR
  pypi[(PyPI)] -->|uv lock: 7-day release age, hashes| lock[uv.lock]
  gh[(GitHub: gitleaks repo)] -->|pinned commit SHA| hooks[Git hooks]
  dev([Developer or AI agent]) -->|commit| hooks
  hooks -->|ruff · gitleaks · private keys · commit rules| repo[(Repository)]
  lock --> venv[Virtual environment]
```

## Threats (STRIDE)

| Element | Threat | Control | Test |
|---|---|---|---|
| Dependencies | Tampering: a freshly published malicious release | `exclude-newer = "7 days"`; `uv.lock` with hashes; Renovate with minimum release age (slice 1.4) | Lock records `exclude-newer-span = "P7D"` |
| Dependencies | Tampering: dependency confusion for internal packages | Internal packages come only from the workspace (`{ workspace = true }`) | `test_workspace_package__import__resolves_to_this_checkout` |
| Test run | Tampering: a dependency's pytest plugin runs code in every test session | Plugins load only from the allow-list (`--disable-plugin-autoload`) | Session header lists only `anyio` and `hypothesis` |
| Repository | Information disclosure: secrets committed | gitleaks and private-key detection on every commit; CI scan from slice 1.4 | `prek run --all-files` |
| Architecture | Elevation of privilege: inner layers gaining I/O or SDK access over time | Layer purity test, import-linter contracts | `tests/architecture/` |
| Hooks | Tampering: a moved Git tag changes hook code | gitleaks hook pinned by full commit SHA | Config review |

## Personal data

None.

## ASVS 5.0 requirements covered

| Requirement | How it is met | Evidence |
|---|---|---|
| v5.0.0-15.1.1 | Update time frames documented: seven-day release age for every dependency; remediation through Renovate (slice 1.4) | [TOOLCHAIN.md](../../engineering/TOOLCHAIN.md) |
| v5.0.0-15.1.2 | `uv.lock` is the inventory of every third-party package, including transitive ones; a CycloneDX SBOM is attached to releases later | `uv.lock` |
| v5.0.0-15.2.4 | Third-party packages resolve only from PyPI; internal packages only from the workspace, so they cannot be confused with a public package | `pyproject.toml` sources, workspace test |
| v5.0.0-13.3.1 (partial) | No secrets in the repository: gitleaks and private-key detection on commit; secret stores arrive with deployment (slice 1.5) | `.pre-commit-config.yaml` |
