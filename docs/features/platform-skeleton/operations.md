# Platform skeleton — Operations

Until slice 1.5 deploys the API, operations means running it locally and keeping the toolchain healthy.

## Configuration

| Setting | Where | Effect |
|---|---|---|
| `HTCO_ENVIRONMENT` | Required: `local`, `test`, `staging`, `production` | Names the environment in logs; later slices switch behaviour on it |
| `HTCO_LOG_LEVEL` | `INFO` (`DEBUG`, `WARNING`, `ERROR`) | Minimum level for every logger; HTTP client loggers stay at WARNING |
| `HTCO_HOST`, `HTCO_PORT` | `127.0.0.1`, `8000` | Listen address |
| `HTCO_SENTRY_DSN` | unset (secret) | Turns error monitoring on; blank means off; never logged |
| `HTCO_DATABASE_URL` | unset locally; **required** in staging and production (secret) | Postgres connection string; `/readyz` checks it with a one-second connect timeout |
| `HTCO_RELEASE` | unset | Build identifier (git SHA) attached to error reports and the `api.starting` line |
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

### Run the production image locally
1. `docker build -t htco-api .`
2. `docker run --rm -p 8080:8080 --read-only --env HTCO_ENVIRONMENT=local htco-api`
3. `curl http://127.0.0.1:8080/healthz`

### The `image` job fails on the vulnerability scan
1. **Check:** the Grype table names the package, the installed version and the fixed version.
2. **Fix:** a Python package: raise its lock (`uv lock --upgrade-package <name>`, respecting the release age); an OS package: take the new base-image digest (Renovate proposes it).
3. **Never** lower the severity cutoff to get green; an exception needs the owner's agreement, an issue and an expiry date.

### The Claude Code stop hook keeps refusing to end a turn
1. **Check:** the hook's message names the failing recipe step and shows the last 60 lines of `just check-fast`.
2. **Fix:** fix the code or the test that is wrong; never weaken a test (ER-TEST-02).
3. **If the failure is environmental** (for example uv cannot reach PyPI): the hook lets the turn end on the next attempt with a visible warning; report it to the owner instead of committing.

### Check that error monitoring works (any environment except production)
1. `curl -X POST https://<api>/_diagnostics/error` and note the `X-Request-Id`.
2. **Expect:** a 500 problem; in the logs, `request.failed` with that request ID and a `trace_id`; in Sentry, one `DiagnosticError` event tagged with both IDs.
3. If the event is missing: check `api.starting` says `error_monitoring: sentry`, then the DSN in the secret store.

### The API exits immediately with code 78
1. **Check:** the last stderr lines: `htco-api: invalid configuration` followed by one line per variable.
2. **Fix:** set or correct the named variables (values are never printed; check them in the secret store).
3. **Verify:** the first log line is `api.starting` with the environment.

### `/readyz` returns 503
1. **Check:** the response's `checks` map names the failing dependency; the `readiness.check_failed` log line has its error type.
2. **Fix:** restore the dependency; the probe recovers by itself on the next call.

### `ci-gate` is red on a pull request
1. **Check:** open the run; `ci-gate` lists each job that did not succeed (`::error::<job>: failure`).
2. **Fix by job:** `checks` → run `uv run just lint types arch` locally; `tests` → `uv run just test`; `security` → remove the secret (and rotate it if it was ever real) or upgrade the vulnerable package; `workflows` → `uv run just workflows`; `test-integrity` → explain the change in the pull request's "Test changes" section (editing the description re-runs CI); `pr-title` → rename the pull request as `type(scope): subject`.
3. **Never** weaken a check to get green (ER-TEST-02); a red security job blocks the merge until fixed.

### A scanner binary needs updating
1. Pick the newest release that is at least seven days old.
2. Update the version and the SHA-256 from the release's published checksum file in `.github/workflows/ci.yml`, and the version in [TOOLCHAIN.md](../../engineering/TOOLCHAIN.md).

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
