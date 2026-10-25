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
# uv's archive is checked against this pin, kept in the repository and reviewed like code; the
# release's own .sha256 file comes from the same origin as the archive, so it proves nothing.
# When CI moves to a new uv, update both lines together (the digest is on the release page and in
# `gh api repos/astral-sh/uv/releases/tags/<version>`, field `digest`).
uv_pinned_version="0.13.0"
uv_pinned_sha256="1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154"

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
    if [ "$uv_version" != "$uv_pinned_version" ]; then
        echo "CI uses uv $uv_version but this script pins the SHA-256 of $uv_pinned_version:" \
            "update uv_pinned_version and uv_pinned_sha256" >&2
        exit 1
    fi
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    base="https://github.com/astral-sh/uv/releases/download/$uv_version"
    archive="uv-x86_64-unknown-linux-gnu.tar.gz"
    curl -fsSL -o "$tmp/$archive" "$base/$archive"
    echo "$uv_pinned_sha256  $tmp/$archive" | sha256sum --check --strict --quiet
    tar -xzf "$tmp/$archive" -C "$tmp"
    install -m 755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$tmp/uv-x86_64-unknown-linux-gnu/uvx" \
        /usr/local/bin/
fi
uv python install "$python_version"

echo "Ready: $(uv --version), Python $python_version, $(just --version), $(git --version)," \
    "$(docker --version)"
