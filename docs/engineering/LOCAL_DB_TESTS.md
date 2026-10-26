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
- The end of the output is a summary with `PASS`, `FAIL`, `TIME` (timed out) or `SKIP` and the
  time of each step, then `database job: PASS` or `FAIL`. The full output is in `~/.cache/htco-local-db-ci/last-run.log`.
- Exit code: 0 when every step passed, 1 when a step failed, 2 for a setup problem (missing tool,
  Docker not starting, not an htco-platform clone).
- `timeout-minutes` is enforced as in CI: each step's own, and the job's (20 minutes for
  `database`). A step past its time is stopped with every process it started, fails as timed out,
  and the rest are skipped. One run at a time holds a lock (`~/.cache/htco-local-db-ci/lock`);
  no step or leftover process inherits it, so a hung or killed run never blocks the next one.
- **Your own stack is safe.** The Supabase project is the clone's (`htco-platform`), the same one
  `just db-start` uses. The script stops only a stack it started itself (it records that in
  `~/.cache/htco-local-db-ci/stack-started`). If another stack of the project is running, it stops
  with exit code 2 and leaves that stack alone: stop it yourself (`supabase stop`) and run again.
- **The cache.** `HTCO_LOCAL_CI_CACHE` (default `~/.cache/htco-local-db-ci`) must be a directory of
  its own: the script refuses `/`, a home directory, a `/mnt` drive, anything that contains or is
  inside the clone under test, and a non-empty directory without its `.htco-local-db-ci` marker,
  because it deletes in it as root.
- `--keep-running` leaves the database on `postgresql://postgres:postgres@127.0.0.1:54322/postgres`
  for more `just test-db` runs; the next run removes it first.

## Trust boundary

`local-db-ci.sh` runs the tested clone's own code (its workflow steps, migrations, tests and
scripts) **as root in WSL, with read and write access to the Windows disk under `/mnt/c`**. Run
it only on team branches of `Tzvi1234/htco-platform` that you would merge. Never run it on a fork,
a contributor's branch or any clone you have not read: anything such a branch does runs with those
rights. `local-db-setup.sh` installs uv and checks it against the SHA-256 pinned in the script itself,
not the release's own `.sha256` file. Both scripts need an **x86_64** machine and stop with exit
code 2 on any other: CI's database job installs the `linux_amd64` Supabase CLI, and the local run
replays that step as it is. The Supabase CLI is checked against the SHA-256 pinned in
`ci.yml`.

## Timing

Measured on 2026-10-23 on the owner's PC (WSL 2, 16 cores, 8 GB for WSL) against `main` at
88c372f; every step passed (pgTAP, then 203 integration tests):

| Run | Time |
|---|---|
| First run on the machine, clone inside WSL | 4m47s (starting Postgres the first time takes 2m16s) |
| Later runs, clone inside WSL | 2m05s |
| Later runs, Windows checkout under `/mnt/c` | 3m24s (`just test-db` is slower on the Windows disk) |
| CI `database` job, for comparison | about 20 minutes |

## Known limits

- A Windows **git worktree** (not a plain clone) can be tested, but git commands inside WSL cannot
  read it, because its `.git` file points to a Windows path. The run itself does not need git; the
  header then shows `?` for the commit.
- Docker images and the uv cache live in the distro; deleting the distro means a new first run.
