#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

LOCAL_IMAGE_TAG="${SERVER_IMAGE_TAG:-savant-server:latest}"
DOCKERHUB_REPO="${DOCKERHUB_REPO:-ashabbir/savant-server}"
RELEASE_TAG="${RELEASE_TAG:-$(date +%F)}"

echo "═══════════════════════════════════════════"
echo "  Savant Server — Build, Restart, Push"
echo "═══════════════════════════════════════════"

echo "→ Building local image: $LOCAL_IMAGE_TAG"
docker build \
  --build-arg SAVANT_UID="$(id -u)" \
  --build-arg SAVANT_GID="$(id -g)" \
  -t "$LOCAL_IMAGE_TAG" .

echo "→ Restarting local compose stack"
docker compose up -d --build --remove-orphans

echo "→ Tagging Docker Hub images"
docker tag "$LOCAL_IMAGE_TAG" "${DOCKERHUB_REPO}:latest"
docker tag "$LOCAL_IMAGE_TAG" "${DOCKERHUB_REPO}:${RELEASE_TAG}"

echo "→ Pushing Docker Hub tags"
docker push "${DOCKERHUB_REPO}:latest"
docker push "${DOCKERHUB_REPO}:${RELEASE_TAG}"

echo ""
echo "✔ Done."
echo "  Local image:   $LOCAL_IMAGE_TAG"
echo "  Docker Hub:    ${DOCKERHUB_REPO}:latest"
echo "  Release tag:   ${DOCKERHUB_REPO}:${RELEASE_TAG}"
