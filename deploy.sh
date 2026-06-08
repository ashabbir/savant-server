#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_TAG="${SERVER_IMAGE_TAG:-savant-server:latest}"
HEALTH_TIMEOUT="${SERVER_HEALTH_TIMEOUT:-60}"

echo "═══════════════════════════════════════════"
echo "  Savant Server — Deploy"
echo "═══════════════════════════════════════════"

# Build image if it doesn't exist
if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  echo "→ Image $IMAGE_TAG not found, building..."
  "$SCRIPT_DIR/build.sh"
fi

# Ensure data directory exists
SAVANT_DATA="${SAVANT_DATA_DIR:-$HOME/.savant/server-data}"
mkdir -p "$SAVANT_DATA"

# Bring up via docker compose
echo "→ Starting containers with docker compose..."
docker compose up -d --remove-orphans

# Wait for health check
echo "→ Waiting for health check (timeout: ${HEALTH_TIMEOUT}s)..."
elapsed=0
healthy=false
while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
  # Check if any service has a health check and if it's healthy
  status=$(docker compose ps --format json 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1 || true)
  if echo "$status" | grep -q '"Health":"healthy"'; then
    healthy=true
    break
  fi

  # Fallback: check if containers are running (for services without healthcheck)
  running=$(docker compose ps --status running --format json 2>/dev/null | head -1 || true)
  if [[ -n "$running" ]] && [[ -z "$status" ]]; then
    # No health check defined — container is running, treat as ready
    healthy=true
    break
  fi

  sleep 2
  elapsed=$((elapsed + 2))
  printf "  %ds...\r" "$elapsed"
done
echo ""

if $healthy; then
  echo "✔ Server is up and running."
else
  echo "⚠ Health check did not pass within ${HEALTH_TIMEOUT}s."
  echo "  Containers may still be starting. Check logs with:"
  echo "    docker compose logs -f"
fi

echo ""
echo "Status:"
docker compose ps
