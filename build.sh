#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_TAG="${SERVER_IMAGE_TAG:-savant-server:latest}"

echo "═══════════════════════════════════════════"
echo "  Savant Server — Build"
echo "═══════════════════════════════════════════"

# Ensure host data directory exists for bind mount
SAVANT_DATA="${SAVANT_DATA_DIR:-$HOME/.savant/server-data}"
mkdir -p "$SAVANT_DATA"
echo "→ Data directory: $SAVANT_DATA"

echo "→ Building Docker image: $IMAGE_TAG ..."
docker build \
  --build-arg SAVANT_UID="$(id -u)" \
  --build-arg SAVANT_GID="$(id -g)" \
  -t "$IMAGE_TAG" \
  -t "ashabbir/savant-server:latest" .

echo ""
echo "✔ Build complete."
echo "  Image: $IMAGE_TAG"
docker images --format "  Size:  {{.Size}}" "$IMAGE_TAG"
docker images --format "  ID:    {{.ID}}" "$IMAGE_TAG"
