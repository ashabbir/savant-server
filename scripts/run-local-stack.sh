#!/usr/bin/env bash
set -euo pipefail

socket_path="${SAVANT_CODEGRAPH_SOCKET:-/tmp/savant-codegraph.sock}"
export SAVANT_CODEGRAPH_SOCKET="$socket_path"
export SAVANT_CODEGRAPH_BASE_ROOTS="${SAVANT_CODEGRAPH_BASE_ROOTS:-${BASE_CODE_DIR:-/Users/home/code}}"

node codegraph_bridge/src/server.js &
bridge_pid=$!
gunicorn_pid=""

shutdown() {
  [[ -z "$gunicorn_pid" ]] || kill "$gunicorn_pid" 2>/dev/null || true
  kill "$bridge_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap shutdown EXIT INT TERM

gunicorn --workers "${GUNICORN_WORKERS:-2}" --bind "${GUNICORN_BIND:-0.0.0.0:8090}" app:app &
gunicorn_pid=$!

while kill -0 "$gunicorn_pid" 2>/dev/null; do
  if ! kill -0 "$bridge_pid" 2>/dev/null; then
    node codegraph_bridge/src/server.js &
    bridge_pid=$!
  fi
  sleep 1
done
wait "$gunicorn_pid"
