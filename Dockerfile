# Extend the published server image so local builds do not redownload the
# large ML/CUDA dependency set just to add the Git runtime dependency.
ARG SAVANT_SERVER_BASE_IMAGE=savant-server:base
FROM node:22.17.0-bookworm-slim AS codegraph-runtime

WORKDIR /bridge
COPY codegraph_bridge/package.json codegraph_bridge/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM ${SAVANT_SERVER_BASE_IMAGE}

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# CodeGraph is an internal implementation detail of the server image. Keep the
# exact Node 22 runtime and production dependency lock in the same artifact.
COPY --from=codegraph-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=codegraph-runtime /bridge/node_modules /app/codegraph_bridge/node_modules

# Overlay the checked-out server source, including the Git ingestion fix.
# .dockerignore excludes local credentials such as .env.
COPY --chown=savant:savant . /app
RUN chmod +x /app/docker-entrypoint.sh

USER savant
