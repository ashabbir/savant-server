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
export SAVANT_EXTERNAL_PERIODIC_RUNNER=1
export SAVANT_EXTERNAL_KG_MAINTENANCE=1
export NODE_OPTIONS="${NODE_OPTIONS:---disable-warning=ExperimentalWarning}"

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

if [ "${SAVANT_ROLE:-}" = "worker" ] || [ "${SAVANT_ROLE:-}" = "jobs" ] || [ "${SAVANT_WORKER_ONLY:-}" = "1" ]; then
  echo "Starting dedicated savant-jobs worker container..."
  python -m context.job_worker &
  JOB_WORKER_PID="$!"
  CHILD_PIDS="$CHILD_PIDS $JOB_WORKER_PID"

  while :; do
    for pid in $CHILD_PIDS; do
      if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || status="$?"
        echo "Required worker process $pid exited" >&2
        exit "${status:-1}"
      fi
    done
    sleep 2
  done
fi

# Run both MCP transports as independent supervised listeners.  SSE remains
# available for long-running legacy clients; modern clients use /mcp over
# Streamable HTTP.  Separate ports avoid changing either wire contract.
MCP_PIDS=""
start_mcp() {
  python "$@" &
  MCP_PIDS="$MCP_PIDS $!"
}

if [ "${SAVANT_MCP_SSE_ENABLED:-true}" = "true" ]; then
  start_mcp /app/mcp/server.py --transport sse --host 0.0.0.0 --port "${SAVANT_MCP_WORKSPACE_PORT:-8091}"
  start_mcp /app/mcp/abilities_server.py --transport sse --host 0.0.0.0 --port "${SAVANT_MCP_ABILITIES_PORT:-8092}"
  start_mcp /app/mcp/context_server.py --transport sse --host 0.0.0.0 --port "${SAVANT_MCP_CONTEXT_PORT:-8093}" --flask-url "${SAVANT_API_BASE}"
  start_mcp /app/mcp/knowledge_server.py --transport sse --host 0.0.0.0 --port "${SAVANT_MCP_KNOWLEDGE_PORT:-8094}"
  start_mcp /app/mcp/reminders_server.py --transport sse --host 0.0.0.0 --port "${SAVANT_MCP_REMINDERS_PORT:-8095}"
fi

if [ "${SAVANT_MCP_STREAMABLE_HTTP_ENABLED:-true}" = "true" ]; then
  start_mcp /app/mcp/server.py --transport streamable-http --host 0.0.0.0 --port "${SAVANT_MCP_STREAMABLE_WORKSPACE_PORT:-8191}"
  start_mcp /app/mcp/abilities_server.py --transport streamable-http --host 0.0.0.0 --port "${SAVANT_MCP_STREAMABLE_ABILITIES_PORT:-8192}"
  start_mcp /app/mcp/context_server.py --transport streamable-http --host 0.0.0.0 --port "${SAVANT_MCP_STREAMABLE_CONTEXT_PORT:-8193}" --flask-url "${SAVANT_API_BASE}"
  start_mcp /app/mcp/knowledge_server.py --transport streamable-http --host 0.0.0.0 --port "${SAVANT_MCP_STREAMABLE_KNOWLEDGE_PORT:-8194}"
  start_mcp /app/mcp/reminders_server.py --transport streamable-http --host 0.0.0.0 --port "${SAVANT_MCP_STREAMABLE_REMINDERS_PORT:-8195}"
fi

CHILD_PIDS="$CHILD_PIDS $MCP_PIDS"

# Run persistent queue consumer only if not offloaded to dedicated savant-jobs worker container.
if [ "${SAVANT_ROLE:-}" != "server" ] && [ "${SAVANT_EXTERNAL_JOB_WORKER:-0}" != "1" ]; then
  nice -n 10 python -m context.job_worker &
  JOB_WORKER_PID="$!"
  CHILD_PIDS="$CHILD_PIDS $JOB_WORKER_PID"
fi

nice -n 10 python -m context.periodic_runner &
PERIODIC_RUNNER_PID="$!"
CHILD_PIDS="$CHILD_PIDS $PERIODIC_RUNNER_PID"

# One dedicated scheduler process owns the four-hour graph optimization cron.
# The transaction advisory lock remains a cross-container guard during deploys.
nice -n 10 python -m knowledge.maintenance_runner &
KG_MAINTENANCE_PID="$!"
CHILD_PIDS="$CHILD_PIDS $KG_MAINTENANCE_PID"

gunicorn \
  --bind "${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8090}" \
  --workers "${GUNICORN_WORKERS:-1}" \
  --threads "${GUNICORN_THREADS:-8}" \
  --preload \
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
