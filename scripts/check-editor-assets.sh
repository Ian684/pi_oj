#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATIC_DIR="$ROOT_DIR/backend/static"
WEB_URL="${WEB_URL:-}"

missing=0
host_missing=0
for path in \
  "$STATIC_DIR/vendor/monaco/vs/loader.js" \
  "$STATIC_DIR/vendor/monaco/vs/editor/editor.main.js" \
  "$STATIC_DIR/vendor/monaco-vim.js" \
  "$STATIC_DIR/vendor/marked.umd.js" \
  "$STATIC_DIR/vendor/katex/katex.min.css" \
  "$STATIC_DIR/vendor/katex/katex.min.js" \
  "$STATIC_DIR/vendor/katex/auto-render.min.js"
do
  if [ -f "$path" ]; then
    echo "OK: ${path#$ROOT_DIR/}"
  else
    echo "HOST MISSING: ${path#$ROOT_DIR/}"
    host_missing=1
  fi
done

if [ -n "$WEB_URL" ]; then
  for url in \
    "$WEB_URL/static/vendor/monaco/vs/loader.js" \
    "$WEB_URL/static/vendor/monaco/vs/editor/editor.main.js" \
    "$WEB_URL/static/vendor/monaco-vim.js" \
    "$WEB_URL/static/vendor/marked.umd.js" \
    "$WEB_URL/static/vendor/katex/katex.min.css" \
    "$WEB_URL/static/vendor/katex/katex.min.js" \
    "$WEB_URL/static/vendor/katex/auto-render.min.js"
  do
    if curl -fsSI "$url" >/dev/null; then
      echo "HTTP OK: $url"
    else
      echo "HTTP MISSING: $url"
      missing=1
    fi
  done
elif [ "$host_missing" -eq 1 ]; then
  echo "Host vendor files may be absent before Docker build; set WEB_URL to verify the running service."
fi

exit "$missing"

