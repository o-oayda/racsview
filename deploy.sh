#!/bin/bash

set -euo pipefail

python3 -m http.server 8000 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

firefox http://localhost:8000/ &

# Keep script alive while the server runs; Ctrl+C will trigger cleanup.
wait "$SERVER_PID"
