#!/usr/bin/env sh
set -eu

NGROK_URL="${NGROK_URL:-https://unremovable-unrepeatable-phuong.ngrok-free.dev}"
UPSTREAM_PORT="${UPSTREAM_PORT:-8000}"
POLICY_FILE="${POLICY_FILE:-ngrok-policy.yaml}"

cd "$(dirname "$0")"

if [ -f "$POLICY_FILE" ]; then
  exec ngrok http --url="$NGROK_URL" --traffic-policy-file "$POLICY_FILE" "$UPSTREAM_PORT"
fi

echo "WARNING: $POLICY_FILE not found; ngrok will start without traffic policy/auth protection." >&2
echo "Only continue for trusted private testing. Put ngrok-policy.yaml at the project root for normal use." >&2
exec ngrok http --url="$NGROK_URL" "$UPSTREAM_PORT"

