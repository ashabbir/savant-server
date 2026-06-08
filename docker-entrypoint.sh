#!/usr/bin/env sh
set -eu

cleanup() {
  for pid in ${MCP_PIDS:-}; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

# Ensure data directories exist (bind mount may be empty on first run)
DATA_DIR="${SAVANT_SERVER_DATA_DIR:-/data/savant}"
mkdir -p "$DATA_DIR/hf" "$DATA_DIR/abilities/personas" "$DATA_DIR/abilities/rules" \
         "$DATA_DIR/abilities/policies" "$DATA_DIR/abilities/repos" 2>/dev/null || true

export SAVANT_API_BASE="${SAVANT_API_BASE:-http://127.0.0.1:${FLASK_PORT:-8090}}"

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

exec gunicorn \
  --bind "${FLASK_HOST:-0.0.0.0}:${FLASK_PORT:-8090}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout 60 \
  app:app
