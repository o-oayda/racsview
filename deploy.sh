#!/bin/bash

set -euo pipefail

python3 server.py 8000 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 1
URL="http://localhost:8000/"
OS="$(uname -s)"

if [ "$OS" = "Darwin" ]; then
  open -a Firefox "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v firefox >/dev/null 2>&1; then
  firefox "$URL" >/dev/null 2>&1 &
else
  echo "Open $URL in your browser."
fi

# Keep script alive while the server runs; Ctrl+C will trigger cleanup.
wait "$SERVER_PID"
