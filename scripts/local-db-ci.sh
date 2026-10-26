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
# CI's database job installs the linux_amd64 Supabase CLI, and its steps are replayed as they are.
machine="$(uname -m)"
case "$machine" in
    x86_64 | amd64) ;;
    *)
        echo "unsupported architecture $machine: CI's database job installs the linux_amd64" \
            "Supabase CLI, so local runs need an x86_64 machine" >&2
        exit 2
        ;;
esac
for tool in uv docker git; do
    command -v "$tool" > /dev/null || {
        echo "$tool is missing: run scripts/local-db-setup.sh first" >&2
        exit 2
    }
done

started=$SECONDS
# The cache is deleted in (as root), so it must be this script's own: a canonical path that is
# not /, a home, a /mnt drive or anything that overlaps the clone, and either new, empty, the
# default location, or already marked as ours.
default_cache="$HOME/.cache/htco-local-db-ci"
cache="$(realpath -m -- "${HTCO_LOCAL_CI_CACHE:-$default_cache}")"
repo_real="$(realpath -- "$repo")"
refuse_cache() {
    echo "HTCO_LOCAL_CI_CACHE=$cache: $1; use a directory of its own (default $default_cache)" >&2
    exit 2
}
owner_marker="$cache/.htco-local-db-ci"
case "$cache" in
    / | /root | /home | /mnt | /mnt/?) refuse_cache "a system, home or drive root" ;;
esac
[ "$(dirname -- "$cache")" = /home ] && refuse_cache "a home directory"
[ "$cache" = "$(realpath -m -- "$HOME")" ] && refuse_cache "the home directory"
case "$cache/" in "$repo_real"/*) refuse_cache "inside the clone under test" ;; esac
case "$repo_real/" in "$cache"/*) refuse_cache "contains the clone under test" ;; esac
if [ -d "$cache" ] && [ ! -e "$owner_marker" ] && [ -n "$(ls -A -- "$cache")" ] \
    && [ "$cache" != "$(realpath -m -- "$default_cache")" ]; then
    refuse_cache "a non-empty directory this script did not create"
fi
mkdir -p -- "$cache"
touch -- "$owner_marker"

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

# The Supabase stack is the clone's project (supabase/config.toml), the same one `just db-start`
# uses: this script stops only a stack it started itself, recorded in $stack_marker.
project_id="$(sed -n 's/^project_id *= *"\([^"]*\)".*/\1/p' "$repo/supabase/config.toml" | head -1)"
[ -n "$project_id" ] || { echo "no project_id in $repo/supabase/config.toml" >&2; exit 2; }
stack_marker="$cache/stack-started"
stack_running() {
    [ -n "$(docker ps -q --filter "label=com.supabase.cli.project=$project_id" 2> /dev/null)" ]
}
# Why this script's own stack could not be stopped, if it could not; its marker is then kept, so
# the next run still knows the stack is its own and tries again.
own_stop_failed=""
stop_stack() {
    [ "$(cat -- "$stack_marker" 2> /dev/null)" = "$project_id" ] || return 0
    local supabase="$runner_temp/supabase"
    [ -x "$supabase" ] || supabase="$(command -v supabase || true)"
    if [ -z "$supabase" ]; then
        own_stop_failed="no Supabase CLI was found to stop it with"
        return 0
    fi
    if ! (cd "$repo" && "$supabase" stop --no-backup > /dev/null 2>&1 9>&-); then
        own_stop_failed="supabase stop --no-backup failed"
        return 0
    fi
    rm -f -- "$stack_marker"
}
refuse_own_stack() {
    echo "this script's own Supabase stack for project $project_id could not be stopped" \
        "($own_stop_failed): stop it with supabase stop --no-backup, then run again" >&2
    exit 2
}
# Start from a clean stack, like a fresh CI runner: a stack left by --keep-running is removed,
# before the runner directory (its CLI may be the only one on the machine). Anyone else's stack
# is left alone, and the run stops: its database would clash with CI's.
stop_stack
if stack_running; then
    [ -z "$own_stop_failed" ] || refuse_own_stack
    echo "a Supabase stack for project $project_id is running that this script did not start" \
        "(just db-start, or another clone): stop it yourself (supabase stop), then run again" >&2
    exit 2
fi
# A stopped stack keeps its data: `just db-stop` (a plain `supabase stop`) leaves the project's
# volumes. Claiming the project would reset them, so they must be gone first (ours already are).
saved_volumes="$(docker volume ls -q --filter "label=com.supabase.cli.project=$project_id" \
    2> /dev/null | tr '\n' ' ')"
if [ -n "${saved_volumes// /}" ]; then
    [ -z "$own_stop_failed" ] || refuse_own_stack
    echo "saved Supabase volumes of project $project_id that this script did not create:" \
        "$saved_volumes(a stopped stack's data, kept by just db-stop). Remove them yourself if" \
        "the data can go (supabase stop --no-backup), then run again" >&2
    exit 2
fi
rm -f -- "$stack_marker"
rm -rf -- "$runner_temp"
# From here the runner's steps start the stack: mark it as ours before they do.
printf '%s\n' "$project_id" > "$stack_marker"
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
