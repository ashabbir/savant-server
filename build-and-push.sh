#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Configurable registry and image name
REGISTRY_IMAGE="${SERVER_REGISTRY_IMAGE:-ashabbir/savant-server}"
DATE_TAG="$(date +%Y-%m-%d)"

echo "═══════════════════════════════════════════"
echo "  Savant Server — Build & Push"
echo "═══════════════════════════════════════════"

# 1. Build the base image
echo "→ Building Docker image as ${REGISTRY_IMAGE}:latest ..."
docker build \
  --build-arg SAVANT_UID="$(id -u)" \
  --build-arg SAVANT_GID="$(id -g)" \
  -t "${REGISTRY_IMAGE}:latest" .

# 2. Tag with the date
echo "→ Tagging image as ${REGISTRY_IMAGE}:${DATE_TAG} ..."
docker tag "${REGISTRY_IMAGE}:latest" "${REGISTRY_IMAGE}:${DATE_TAG}"

# 3. Push to registry
echo "→ Pushing ${REGISTRY_IMAGE}:latest to registry ..."
docker push "${REGISTRY_IMAGE}:latest"

echo "→ Pushing ${REGISTRY_IMAGE}:${DATE_TAG} to registry ..."
docker push "${REGISTRY_IMAGE}:${DATE_TAG}"

echo ""
echo "✔ Build and Push complete."
echo "  Images pushed:"
echo "    - ${REGISTRY_IMAGE}:latest"
echo "    - ${REGISTRY_IMAGE}:${DATE_TAG}"
