#!/bin/bash

set -euo pipefail

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  PYTHON_BIN="uv run python"
else
  PYTHON_BIN="python3"
fi

$PYTHON_BIN server.py 8000 &
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
