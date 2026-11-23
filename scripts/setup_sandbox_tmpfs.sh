#!/usr/bin/env bash
# Optional host-level protection: cap *total* sandbox temporary file storage.
# Run on Raspberry Pi HOST, only when no judge task is running:
#   sudo bash scripts/setup_sandbox_tmpfs.sh 512M
# Requires worker restart after mounting. This mount is lost at reboot.
set -euo pipefail
SIZE="${1:-512M}"
if [[ ! "$SIZE" =~ ^[0-9]+[MG]$ ]]; then
  echo 'Usage: sudo bash scripts/setup_sandbox_tmpfs.sh 512M' >&2
  exit 2
fi
if (( EUID != 0 )); then
  echo 'Run this helper with sudo on Raspberry Pi host.' >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$ROOT/backend/sandboxes"
if [[ -f "$ROOT/.env" ]]; then
  ENV_PATH="$(sed -n 's/^HOST_SANDBOX_ROOT=//p' "$ROOT/.env" | tail -1)"
  if [[ -n "$ENV_PATH" && "$ENV_PATH" != "$TARGET" ]]; then
    echo "HOST_SANDBOX_ROOT=$ENV_PATH does not match $TARGET. Refusing mount." >&2
    echo 'Adjust your host sandbox path first, then verify Docker Compose mount paths.' >&2
    exit 2
  fi
fi
mkdir -p "$TARGET"
if mountpoint -q "$TARGET"; then
  if [[ "$(findmnt -n -o FSTYPE --target "$TARGET")" == "tmpfs" ]]; then
    echo "Already a tmpfs mount point: $TARGET"
    exit 0
  fi
  echo "Existing mount is not tmpfs. Refusing to claim quota protection." >&2
  exit 2
fi
if find "$TARGET" -mindepth 1 -maxdepth 1 ! -name .gitkeep -print -quit | grep -q .; then
  echo 'Sandbox directory contains files. Finish all jobs and clean stale data first.' >&2
  exit 2
fi
echo "Mounting $SIZE tmpfs at $TARGET. Data here is ephemeral."
mount -t tmpfs -o "size=$SIZE,mode=1777,nodev,nosuid" tmpfs "$TARGET"
echo 'Host sandbox tmpfs is mounted.'
echo 'Restart Worker: docker compose up -d --force-recreate worker'
echo 'NOTE: This tmpfs mount does NOT survive reboot. Arrange a systemd mount unit separately if needed.'
