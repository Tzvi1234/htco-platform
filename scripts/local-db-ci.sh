#!/usr/bin/env bash
# Run CI's `database` job locally in WSL Ubuntu (docs/engineering/LOCAL_DB_TESTS.md):
#   wsl -d Ubuntu -u root -- bash <repo>/scripts/local-db-ci.sh [REPO] [--keep-running]
# REPO defaults to the clone this script is in; it may be a Windows clone under /mnt/c or a
# WSL-side clone (faster). The steps come from .github/workflows/ci.yml through
# tools/local_db_ci.py, so this script only prepares the machine and cleans up.
# Exit code: 0 when every step passed, 1 when one failed, 2 for a setup problem.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
keep_running=false
for argument in "$@"; do
    case "$argument" in
        --keep-running) keep_running=true ;;
        -h | --help) sed -n '2,7p' "${BASH_SOURCE[0]}" | cut -c3-; exit 0 ;;
        -*) echo "unknown option: $argument" >&2; exit 2 ;;
        *) repo="$(cd "$argument" && pwd)" ;;
    esac
done
if [ ! -f "$repo/.github/workflows/ci.yml" ] || [ ! -f "$repo/supabase/config.toml" ]; then
    echo "not an htco-platform clone: $repo" >&2
    exit 2
fi
for tool in uv docker git; do
    command -v "$tool" > /dev/null || {
        echo "$tool is missing: run scripts/local-db-setup.sh first" >&2
        exit 2
    }
done

started=$SECONDS
cache="${HTCO_LOCAL_CI_CACHE:-$HOME/.cache/htco-local-db-ci}"
mkdir -p "$cache"

# Every clone uses the same Supabase project ID, so the containers clash: one run at a time.
exec 9> "$cache/lock"
if ! flock --nonblock 9; then
    echo "another local database run is in progress; waiting for it..." >&2
    flock 9
fi

if ! docker info > /dev/null 2>&1; then
    echo "starting Docker..."
    service docker start > /dev/null 9>&-  # dockerd must not inherit the lock
    for _ in $(seq 30); do docker info > /dev/null 2>&1 && break; sleep 1; done
    docker info > /dev/null 2>&1 || { echo "Docker did not start" >&2; exit 2; }
fi

# A Linux virtual environment outside the clone, so a Windows clone keeps its own .venv.
repo_key="$(printf '%s' "$repo" | sha256sum | cut -c1-12)"
export UV_PROJECT_ENVIRONMENT="$cache/venv-$repo_key"
runner_temp="$cache/runner"

stop_stack() {
    local supabase="$runner_temp/supabase"
    [ -x "$supabase" ] || supabase="$(command -v supabase || true)"
    [ -n "$supabase" ] && (cd "$repo" && "$supabase" stop --no-backup > /dev/null 2>&1 9>&-) || true
}
# Start from a clean stack, like a fresh CI runner (a stack left by --keep-running is removed).
# Stop it before clearing the runner directory: its CLI may be the only one on the machine.
stop_stack
rm -rf "$runner_temp"
if [ "$keep_running" = false ]; then
    # Keep the script's own exit code: the cleanup's status must not replace a failure.
    trap 'code=$?; stop_stack; exit "$code"' EXIT
fi

echo "local database CI for $repo ($(git -C "$repo" rev-parse --short HEAD 2> /dev/null || echo '?'))"
status=0
# The runner comes from this script's clone, outside the target's project, so any branch of any
# clone can be tested, including branches older than the runner.
runner="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tools/local_db_ci.py"
pyyaml="$(awk -F'"' 'found { print $2; exit } $0 == "name = \"pyyaml\"" { found = 1 }' \
    "$(dirname "$runner")/../uv.lock")"
# `9>&-`: the runner and every step start without the lock's descriptor, so a process a step
# leaves behind can never keep the lock (the runner enforces timeout-minutes on the steps).
(cd "$repo" && uv run --quiet --no-project --with "pyyaml==${pyyaml:?no pyyaml in uv.lock}" \
    python "$runner" --root . --runner-temp "$runner_temp" --log "$cache/last-run.log") 9>&- \
    || status=$?
echo "total wall time including setup: $(( (SECONDS - started) / 60 ))m$(( (SECONDS - started) % 60 ))s;" \
    "full log: $cache/last-run.log"
exit "$status"
