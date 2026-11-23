#!/usr/bin/env bash
# Persist a shared Pi OJ sandbox tmpfs across Linux host reboots.
# Commands: status | install [512M] | remove
# Intended for Linux Docker host, not WSL checkout in /mnt/c/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGET="$ROOT/backend/sandboxes"
TAG="# PI_OJ_PERSISTENT_SANDBOX_QUOTA"
CMD="${1:-status}"
SIZE="${2:-512M}"

abort(){ echo "ERROR: $*" >&2; exit 2; }
if [[ "$CMD" == status ]]; then
  exec bash "$ROOT/scripts/check_sandbox_quota.sh"
fi
[[ "$CMD" == install || "$CMD" == remove ]] || abort 'Usage: sudo bash scripts/manage_sandbox_quota.sh install 512M | remove | status'
(( EUID == 0 )) || abort 'Run install/remove with sudo on the Linux Docker host.'
command -v findmnt >/dev/null || abort 'findmnt (util-linux) is required.'
command -v mountpoint >/dev/null || abort 'mountpoint (util-linux) is required.'
[[ "$TARGET" != *[[:space:]]* ]] || abort 'Paths with whitespace are not supported in /etc/fstab.'
[[ "$ROOT" != /mnt/[a-zA-Z]/* ]] || abort 'Do not mount a persistent Linux tmpfs in a WSL Windows-drive checkout.'
[[ "$ROOT" != /proc/* && "$ROOT" != /sys/* ]] || abort 'Unsupported mount target.'
if [[ -f "$ROOT/.env" ]]; then
  CONFIGURED="$(sed -n 's/^HOST_SANDBOX_ROOT=//p' "$ROOT/.env" | tail -n 1)"
  [[ -z "$CONFIGURED" || "$CONFIGURED" == "$TARGET" ]] || abort "HOST_SANDBOX_ROOT=$CONFIGURED differs from $TARGET; fix .env first."
else
  abort 'Missing .env; configure HOST_SANDBOX_ROOT first.'
fi
if command -v docker >/dev/null; then
  docker info >/dev/null 2>&1 || abort 'Cannot reach Docker engine. Verify daemon before changing mounts.'
  if docker ps --filter 'name=^/oj_celery_worker$' --format '{{.Names}}' | grep -qx oj_celery_worker; then
    abort 'Worker is running. Stop it first: docker compose stop worker'
  fi
fi
mkdir -p "$TARGET"

if [[ "$CMD" == install ]]; then
  [[ "$SIZE" =~ ^[1-9][0-9]*[MG]$ ]] || abort 'Size must be like 512M or 1G.'
  if findmnt --fstab --mountpoint "$TARGET" -n >/dev/null 2>&1 && ! grep -Fq "$TAG" /etc/fstab; then
    abort "An untagged /etc/fstab entry already targets $TARGET; inspect it manually."
  fi
  if grep -Fq "$TAG" /etc/fstab; then
    bash "$ROOT/scripts/check_sandbox_quota.sh" || abort 'Quota entry exists but active mount is missing or unsafe; inspect /etc/fstab.'
    echo 'Persistent quota already configured.'
    exit 0
  fi
  if mountpoint -q "$TARGET"; then
    [[ "$(findmnt --mountpoint "$TARGET" -n -o FSTYPE)" == tmpfs ]] || abort 'Existing mount is not tmpfs.'
    abort 'A temporary tmpfs is already mounted. Unmount it after stopping jobs and cleaning, then install persistent quota.'
  fi
  if find "$TARGET" -mindepth 1 -maxdepth 1 ! -name .gitkeep -print -quit | grep -q .; then
    abort 'Sandbox directory contains files; finish jobs, inspect/backup, and clean it before mounting.'
  fi
  BACKUP="/etc/fstab.pi-oj-backup.$(date +%Y%m%d%H%M%S)"
  cp -p /etc/fstab "$BACKUP"
  printf '\n%s\ntmpfs %s tmpfs rw,nosuid,nodev,size=%s,mode=1777 0 0\n' "$TAG" "$TARGET" "$SIZE" >> /etc/fstab
  if command -v systemctl >/dev/null; then systemctl daemon-reload 2>/dev/null || true; fi
  if ! mount "$TARGET"; then
    cp -p "$BACKUP" /etc/fstab
    if command -v systemctl >/dev/null; then systemctl daemon-reload 2>/dev/null || true; fi
    abort "Mount failed. /etc/fstab restored from $BACKUP"
  fi
  echo "Installed persistent tmpfs quota ($SIZE) at $TARGET"
  echo "Saved /etc/fstab backup: $BACKUP"
  bash "$ROOT/scripts/check_sandbox_quota.sh"
  echo 'Now recreate Worker: docker compose up -d --force-recreate worker'
  exit 0
fi

# Removal is intentionally conservative; do not unmount active jobs or discard data.
[[ "$(grep -Fc "$TAG" /etc/fstab || true)" == 1 ]] || abort 'Expected exactly one Pi OJ quota entry in /etc/fstab.'
if mountpoint -q "$TARGET"; then
  [[ "$(findmnt --mountpoint "$TARGET" -n -o FSTYPE)" == tmpfs ]] || abort 'Mount is not tmpfs.'
  if find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    abort 'Mounted tmpfs contains files. Wait for jobs to finish; inspect/remove them manually first.'
  fi
  umount "$TARGET" || abort 'Unmount failed; /etc/fstab unchanged.'
fi
BACKUP="/etc/fstab.pi-oj-backup.$(date +%Y%m%d%H%M%S)"
cp -p /etc/fstab "$BACKUP"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
# Remove only the marker and its immediately following matching mount row.
# Reject malformed neighboring content rather than deleting unrelated entries.
awk -v tag="$TAG" -v target="$TARGET" '
  $0 == tag {
    if (getline next_line <= 0) { exit 3 }
    split(next_line, f, /[[:space:]]+/)
    if (f[1] != "tmpfs" || f[2] != target || f[3] != "tmpfs") { exit 3 }
    seen++
    next
  }
  { print }
  END { if (seen != 1) exit 3 }
' /etc/fstab > "$TMP" || abort 'Malformed tagged fstab entry; nothing removed.'
cat "$TMP" > /etc/fstab
if command -v systemctl >/dev/null; then systemctl daemon-reload 2>/dev/null || true; fi
echo "Removed quota mount and /etc/fstab entry. Backup: $BACKUP"
echo 'WARNING: local judging now fails closed until you install a safe tmpfs.'
