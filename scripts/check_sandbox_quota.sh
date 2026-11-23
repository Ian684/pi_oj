#!/usr/bin/env bash
# Check the host's persistent quota mount; safe for non-root users.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGET="$ROOT/backend/sandboxes"
if [[ -f "$ROOT/.env" ]]; then
  CONFIGURED="$(sed -n 's/^HOST_SANDBOX_ROOT=//p' "$ROOT/.env" | tail -n 1)"
  if [[ -n "$CONFIGURED" && "$CONFIGURED" != "$TARGET" ]]; then
    printf 'FAIL: HOST_SANDBOX_ROOT (%s) does not match %s\n' "$CONFIGURED" "$TARGET" >&2
    exit 1
  fi
fi
if ! mountpoint -q "$TARGET"; then
  echo "FAIL: $TARGET is not a mount point (no disk quota)." >&2
  exit 1
fi
TYPE="$(findmnt --mountpoint "$TARGET" -n -o FSTYPE)"
if [[ "$TYPE" != tmpfs ]]; then
  echo "FAIL: $TARGET is $TYPE, not tmpfs." >&2
  exit 1
fi
printf 'OK: %s is a tmpfs mount.\n' "$TARGET"
findmnt --mountpoint "$TARGET" -o TARGET,FSTYPE,SIZE,AVAIL,OPTIONS
if grep -Fq "# PI_OJ_PERSISTENT_SANDBOX_QUOTA" /etc/fstab; then
  echo 'OK: fstab contains a Pi OJ quota entry; reboot persistence configured.'
else
  echo 'WARNING: no tagged /etc/fstab quota entry; the mount may be temporary.' >&2
  exit 1
fi
