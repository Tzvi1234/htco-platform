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
| `HTCO_SUPABASE_URL` | unset locally; **required** (HTTPS) in staging and production | Supabase project URL; the API verifies access tokens against `<url>/auth/v1/.well-known/jwks.json` with issuer `<url>/auth/v1`. Authentication is on only when this and `HTCO_DATABASE_URL` are set (`authentication` in `api.starting`); otherwise authenticated routes answer 503 |
| `HTCO_SUPABASE_SECRET_KEY` | unset (secret) | Supabase secret key, used only for the Auth admin `invite` call (slice 2.6), never for database queries; without it invitations answer 503 (`invitations` in `api.starting`) |
| `HTCO_SESSION_KEY` | unset (secret) | Seals web sessions' Supabase tokens (32 random bytes, base64); with `HTCO_SUPABASE_PUBLISHABLE_KEY` it turns web sign-in on (`web_sign_in` in `api.starting`) |
| `HTCO_SUPABASE_PUBLISHABLE_KEY` | unset | The project's publishable key for Supabase Auth calls (not secret) |
| `HTCO_WEB_ORIGINS` | empty | Comma-separated web app origins allowed to send cookie requests (CSRF check) |
| `HTCO_INVITE_REDIRECT_URL` | unset | Where the one-time invitation link lands (the web app's accept page; production: `https://dash.ht-co.app/accept-invite`, which must be in Supabase Auth's redirect allow-list) |
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

## Deploying

The API runs on DigitalOcean App Platform as the app `htco` (Frankfurt) at `https://api.ht-co.app` (Cloudflare DNS: `api` CNAME to the app's platform address, DNS only, so App Platform manages the certificate), defined in `infra/digitalocean/app.yaml`: one `apps-s-1vcpu-0.5gb` instance at $5 per month that cannot scale, readiness on `/readyz`, liveness on `/healthz`, alerts for failed deployments, CPU and memory above 80 % and more than three restarts in five minutes.

1. Actions → **Deploy API** → **Run workflow** on `main` with `image_tag` empty.
2. The workflow builds the image, checks it runs as non-root, scans it, pushes `ghcr.io/tzvi1234/htco-api:<commit SHA>`, deploys it with the spec (secrets filled in at deploy time from GitHub secrets, never committed) and calls `/healthz` and `/readyz` on the app's URL. The run summary shows the tag and URL.
3. The database connection uses Supabase's session pooler (IPv4) as `postgres.<project ref>`; the password reaches the app only as the `PGPASSWORD` secret, which libpq reads, so it never appears in a connection string.

Configuration: variables `SUPABASE_PROJECT_REF`, `SUPABASE_POOLER_HOST`, `HTCO_SUPABASE_URL`; secrets `DIGITALOCEAN_ACCESS_TOKEN`, `GHCR_PULL_TOKEN` (`read:packages`, lets DigitalOcean pull the private image), `SUPABASE_DB_PASSWORD`, `HTCO_SENTRY_DSN`, `HTCO_SUPABASE_SECRET_KEY`.

## Rollback

Code: revert the pull request. Deployment: run **Deploy API** with `image_tag` set to the commit SHA of the last good deployment (from that run's summary); the image is already in GHCR, so nothing is rebuilt.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
| 2026-10-14 | Deliberate forbidden import and uncovered domain code (AC-1.1.2, AC-1.1.4) | Gates failed as designed | None |
| 2026-10-14 | First deploy and rollback (AC-1.5.1, AC-1.5.3): deployed `165f0e0`, then `9559d67` (review fixes); rolled back to `165f0e0` with `image_tag` (run 38625634938, no rebuild, started with database, sign-in and invitations on); rolled forward to `9559d672…` (run 38631041130). `/healthz` 200 and `/readyz` `database: ok` after each step | Pass | A shortened SHA (`9559d67c`) reached DigitalOcean, which refused it ("image tag not found") and kept the running version; the workflow now requires the full 40-character SHA |
