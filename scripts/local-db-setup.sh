#!/usr/bin/env bash
# One-time setup of a WSL Ubuntu (or any Debian/Ubuntu) machine for scripts/local-db-ci.sh.
# Installs Docker, git and just from apt, and uv and Python at the versions CI uses. The Supabase
# CLI is not installed here: local-db-ci.sh runs CI's own install step, with its pinned SHA-256.
# Run as root: wsl -d Ubuntu -u root -- bash <repo>/scripts/local-db-setup.sh
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ci="$repo/.github/workflows/ci.yml"
uv_version="$(grep -m1 -oP 'version: "\K[0-9.]+(?=")' "$ci")"
python_version="$(tr -d '[:space:]' < "$repo/.python-version")"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git just docker.io > /dev/null

# Docker starts with the distro: through systemd when /etc/wsl.conf enables it, else at boot.
if [ "$(ps -o comm= -p 1)" = "systemd" ]; then
    systemctl enable --now docker > /dev/null
else
    if grep -q '^command *=' /etc/wsl.conf 2> /dev/null; then
        grep -q 'service docker start' /etc/wsl.conf \
            || echo "add 'service docker start' to the [boot] command in /etc/wsl.conf" >&2
    elif grep -q '^\[boot\]' /etc/wsl.conf 2> /dev/null; then
        sed -i '/^\[boot\]/a command = service docker start' /etc/wsl.conf
    else
        printf '\n[boot]\ncommand = service docker start\n' >> /etc/wsl.conf
    fi
    service docker start
fi

if [ "$(uv --version 2> /dev/null | cut -d' ' -f2)" != "$uv_version" ]; then
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    base="https://github.com/astral-sh/uv/releases/download/$uv_version"
    archive="uv-x86_64-unknown-linux-gnu.tar.gz"
    curl -fsSL -o "$tmp/$archive" "$base/$archive"
    curl -fsSL -o "$tmp/$archive.sha256" "$base/$archive.sha256"
    (cd "$tmp" && sha256sum --check --strict "$archive.sha256")
    tar -xzf "$tmp/$archive" -C "$tmp"
    install -m 755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$tmp/uv-x86_64-unknown-linux-gnu/uvx" \
        /usr/local/bin/
fi
uv python install "$python_version"

echo "Ready: $(uv --version), Python $python_version, $(just --version), $(git --version)," \
    "$(docker --version)"
