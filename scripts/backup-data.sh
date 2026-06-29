#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/pi-oj-backup-$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"
cd "$ROOT_DIR"

INCLUDES="docker-compose.yml README.md start-ngrok.sh ngrok-policy.example.yaml backend/static/cpe-stars.js"

includes_env=0
if [ -f ".env" ]; then
  INCLUDES="$INCLUDES .env"
  includes_env=1
fi

if [ -f "ngrok-policy.yaml" ]; then
  INCLUDES="$INCLUDES ngrok-policy.yaml"
fi

tar -czf "$OUT" $INCLUDES
echo "Backup written to: $OUT"
if [ "$includes_env" -eq 1 ]; then
  echo "WARNING: this backup includes .env secrets. Do not upload it to a public repository or share it with others."
fi
echo "Note: browser localStorage, including CPE exam history, must still be exported from the web UI."

