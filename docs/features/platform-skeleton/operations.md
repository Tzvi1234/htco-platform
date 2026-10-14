# Platform skeleton — Operations

Until slice 1.5 deploys the API, "operations" means keeping the development toolchain healthy.

## Configuration

| Setting | Where | Effect |
|---|---|---|
| Python version | `.python-version` (3.14.8) | uv installs and uses exactly this interpreter |
| uv version | `[tool.uv] required-version` | Any other uv version refuses to run on the workspace |
| Release age | `[tool.uv] exclude-newer = "7 days"` | Resolution ignores versions younger than seven days |
| Coverage floors | `justfile` recipe `test`, `[tool.coverage.report] fail_under` | Domain 95%, application 90%, total 80% (branch coverage) |
| Architecture contracts | `[tool.importlinter]`, `tests/architecture/` | Changes to either need a "Test changes" section in the pull request |

## Runbook

### A Git hook fails on commit
1. **Check:** the hook output names the failing hook.
2. **Likely cause:** formatting (`ruff format` already rewrote the file: stage it again), a lint error, a secret-like string, or a non-conventional commit message.
3. **Fix:** stage the reformatted files; fix lint errors; remove the secret and rotate it if it was real; reword the message as `type(scope): subject`.
4. **Verify:** commit again. Never bypass hooks with `--no-verify`; CI runs the same checks.

### `uv` refuses to run ("required version")
1. **Check:** `uv --version`.
2. **Fix:** `uv self update 0.13.0` (or the version in `pyproject.toml`).

### mypy fails to start with "The filename or extension is too long" (Windows)
1. **Likely cause:** the checkout lives in a very deep folder; compiled mypy modules cannot load from long paths even when long paths are enabled.
2. **Fix:** clone to a shorter path, or point `UV_PROJECT_ENVIRONMENT` at a short folder.

### A new dependency version is not picked up
1. **Likely cause:** it is younger than seven days.
2. **Fix:** wait; for an urgent security fix, add a per-package `exclude-newer-package` entry with a comment and an issue link.

## Rollback

Revert the pull request; there is no runtime state.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
| 2026-10-14 | Deliberate forbidden import and uncovered domain code (AC-1.1.2, AC-1.1.4) | Gates failed as designed | None |
