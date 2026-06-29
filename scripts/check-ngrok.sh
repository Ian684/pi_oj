#!/usr/bin/env sh
set -eu

WEB_URL="${WEB_URL:-http://127.0.0.1:8000}"
NGROK_API="${NGROK_API:-http://127.0.0.1:4040/api/tunnels}"

echo "Checking Pi OJ web service: $WEB_URL"
if curl -fsS "$WEB_URL/api/health" >/dev/null; then
  echo "web: OK"
else
  echo "web: FAILED"
  exit 1
fi

echo "Checking ngrok local API: $NGROK_API"
if ! body="$(curl -fsS "$NGROK_API")"; then
  echo "ngrok: FAILED - is ngrok running?"
  exit 1
fi

public_url="$(printf '%s' "$body" | sed -n 's/.*"public_url":"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -z "$public_url" ]; then
  echo "ngrok: FAILED - no public tunnel found"
  exit 1
fi

echo "ngrok: OK"
echo "public url: $public_url"

