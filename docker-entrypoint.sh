#!/usr/bin/env sh
set -eu

cleanup() {
  for pid in ${CHILD_PIDS:-}; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Ensure data directories exist (bind mount may be empty on first run)
DATA_DIR="${SAVANT_SERVER_DATA_DIR:-/data/savant}"
mkdir -p "$DATA_DIR/hf" "$DATA_DIR/abilities/personas" "$DATA_DIR/abilities/rules" \
         "$DATA_DIR/abilities/policies" "$DATA_DIR/abilities/repos" 2>/dev/null || true

export SAVANT_API_BASE="${SAVANT_API_BASE:-http://127.0.0.1:${FLASK_PORT:-8090}}"
export SAVANT_APP_NAME="${SAVANT_APP_NAME:-savant-mcp}"

# Start the private CodeGraph bridge before API workers. Its Unix socket lives
# only inside this container and is never exposed as a service or port.
mkdir -p /run/savant
rm -f "${SAVANT_CODEGRAPH_SOCKET:-/run/savant/codegraph.sock}"
node /app/codegraph_bridge/src/server.js &
BRIDGE_PID="$!"
CHILD_PIDS="$BRIDGE_PID"
i=0
until node /app/codegraph_bridge/src/healthcheck.js; do
  i=$((i + 1))
  if [ "$i" -ge 30 ] || ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "CodeGraph bridge failed to become ready" >&2
    exit 1
  fi
  sleep 1
done

# Start MCP servers on server-side ports
# Use SSE transport for compatibility with Copilot CLI; override with MCP_TRANSPORT env var
MCP_TRANSPORT="${MCP_TRANSPORT:-sse}"
python /app/mcp/server.py --transport "$MCP_TRANSPORT" --host 0.0.0.0 --port "${SAVANT_MCP_WORKSPACE_PORT:-8091}" &
MCP_PIDS="$!"
python /app/mcp/abilities_server.py --transport "$MCP_TRANSPORT" --host 0.0.0.0 --port "${SAVANT_MCP_ABILITIES_PORT:-8092}" &
MCP_PIDS="$MCP_PIDS $!"
python /app/mcp/context_server.py --transport "$MCP_TRANSPORT" --host 0.0.0.0 --port "${SAVANT_MCP_CONTEXT_PORT:-8093}" --flask-url "${SAVANT_API_BASE}" &
MCP_PIDS="$MCP_PIDS $!"
python /app/mcp/knowledge_server.py --transport "$MCP_TRANSPORT" --host 0.0.0.0 --port "${SAVANT_MCP_KNOWLEDGE_PORT:-8094}" &
MCP_PIDS="$MCP_PIDS $!"
python /app/mcp/reminders_server.py --transport "$MCP_TRANSPORT" --host 0.0.0.0 --port "${SAVANT_MCP_REMINDERS_PORT:-8095}" &
MCP_PIDS="$MCP_PIDS $!"

CHILD_PIDS="$CHILD_PIDS $MCP_PIDS"

gunicorn \
  --bind "${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8090}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout 60 \
  app:app &
GUNICORN_PID="$!"
CHILD_PIDS="$CHILD_PIDS $GUNICORN_PID"

# BusyBox/dash do not provide a portable `wait -n`; supervise every child and
# terminate the whole process group as soon as any required process exits.
while :; do
  for pid in $CHILD_PIDS; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || status="$?"
      echo "Required child process $pid exited" >&2
      exit "${status:-1}"
    fi
  done
  sleep 2
done
