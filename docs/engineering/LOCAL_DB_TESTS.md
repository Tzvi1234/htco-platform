# Local database tests (CI's `database` job on your machine)

| | |
|---|---|
| **Scripts** | [`scripts/local-db-setup.sh`](../../scripts/local-db-setup.sh) (once per machine), [`scripts/local-db-ci.sh`](../../scripts/local-db-ci.sh) (every run) |
| **Runner** | [`tools/local_db_ci.py`](../../tools/local_db_ci.py), tested in `tests/tools/test_local_db_ci.py` |
| **Where** | WSL 2 Ubuntu with Docker Engine inside the distro (`apt docker.io`). Docker Desktop is not needed. Any Debian or Ubuntu Linux works the same way. |

## Why

The `database` job in `.github/workflows/ci.yml` (migrations, seed, pgTAP, the automation
registry sync and `just test-db`) used to be runnable only in CI. This runs the same job locally, so
a developer or an agent can find a failing migration or pgTAP test in minutes instead of waiting
for CI.

## How it stays the same as CI

`local-db-ci.sh` does not copy the job's commands. `tools/local_db_ci.py` reads the `database` job
from `ci.yml` and runs each `run:` step the way a GitHub runner does: `bash --noprofile --norc -eo
pipefail`, the workflow, job and step `env`, `CI=true`, `RUNNER_TEMP` and `GITHUB_PATH`. So CI's own
"Install the Supabase CLI" step downloads the pinned version and checks its SHA-256. Two actions
are handled locally: `actions/checkout` is skipped (you give it a clone) and `astral-sh/setup-uv`
becomes a check that the pinned uv version is installed. Any other action makes the runner stop
with an error, so a new step in the job cannot be silently skipped. As in CI, the first failing
step stops the job.

The wrapper adds what a fresh CI runner gets for free:

- it starts Docker if it is not running;
- it stops any Supabase stack left from an earlier run, and stops the stack at the end (unless
  `--keep-running`);
- it keeps the Linux virtual environment outside the clone (`~/.cache/htco-local-db-ci/venv-…`),
  so a Windows clone's `.venv` is not touched;
- it allows one run at a time, because every clone uses the same Supabase project ID and container
  names. A second run waits for the first.

## Setup (once per machine)

From Windows, with WSL 2 and an Ubuntu distro:

```bash
wsl -d Ubuntu -u root -- bash /mnt/c/<path to clone>/scripts/local-db-setup.sh
```

It installs Docker, git and just from apt, and uv and Python at the versions CI and
`.python-version` pin, and makes Docker start with the distro (systemd when `/etc/wsl.conf` has
`[boot] systemd=true`, otherwise a `[boot] command`). It changes nothing on Windows.

## Run

```bash
wsl -d Ubuntu -u root -- bash /mnt/c/<path to clone>/scripts/local-db-ci.sh /mnt/c/<path to the clone to test>
```

- The clone to test can be a Windows clone under `/mnt/c/…` or a clone inside WSL (for example
  `/root/htco-platform`), which is faster because Docker and uv read files from the Linux disk.
- Without a path, it tests the clone the script is in.
- The runner always comes from the script's clone, so you can test a branch that is older than the
  runner.
- The end of the output is a summary with `PASS`, `FAIL` or `SKIP` and the time of each step, then
  `database job: PASS` or `FAIL`. The full output is in `~/.cache/htco-local-db-ci/last-run.log`.
- Exit code: 0 when every step passed, 1 when a step failed, 2 for a setup problem (missing tool,
  Docker not starting, not an htco-platform clone).
- `--keep-running` leaves the database on `postgresql://postgres:postgres@127.0.0.1:54322/postgres`
  for more `just test-db` runs; the next run removes it first.

## Timing

Measured on the owner's PC (WSL 2, 16 cores, 8 GB for WSL) against `main` at 88c372f:

| Run | Time |
|---|---|
| First run (pulls the Supabase images) | see the pull request |
| Later runs | see the pull request |
| CI `database` job, for comparison | about 20 minutes |
